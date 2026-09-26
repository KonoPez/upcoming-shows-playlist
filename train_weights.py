#!/usr/bin/env python3
"""
Fit the relative weights of the setlist, Last.fm and recency track signals
against what artists actually went on to play live.

Usage:
    python train_weights.py fetch [--max-requests N] [--max-discographies N]
    python train_weights.py fit [--loss ratio|listnet]

Each training example is one artist at one cutoff date. The inputs are the
three signals exactly as `score_track` would have seen them on that date —
setlist frequency replays `setlist_frequencies` over shows *before* the cutoff,
recency is measured from the cutoff — and the target is how often each song
was played in the shows that followed it. Without that split the setlist
signal would be both an input and the answer, and would take all the weight.

Two losses, chosen with `fit --loss`:
  ratio    (default) a track's score over its discography's best score,
           against its future play frequency over the most-played song's,
           as squared error. Scale-invariant, so weights live on the simplex.
  listnet  cross-entropy between the play distribution and a softmax over
           the discography's scores.
Both compare tracks only within their artist — all `select_tracks_for_artist`
ever does. They disagree, though: most tracks are never played, so the ratio
loss rewards scoring the long tail near zero, and it can favour weights that
rank the top songs badly. Read its numbers against p@K and NDCG.

Two models are fit, one per regime `score_track` distinguishes: with setlist
data (setlist, Last.fm, recency) and without it (Last.fm, recency). Novelty is
left out as taste rather than prediction; the printed constants rescale the
learned ratios to fill the 1 - NOVELTY_W that the other signals share.

Caveats worth reading before trusting the numbers:
  - Last.fm play counts are today's, not the cutoff's, so songs that became
    live staples later also gained plays — Last.fm is flattered.
  - The no-setlist model is fit on artists setlist.fm *does* cover, with the
    setlist signal hidden. Uncovered artists are usually smaller.
  - setlist.fm's API terms allow only short-term caching of their data, so the
    history expires with the same 7-day TTL as the production setlist cache.
    Fetch and fit within the same week.
"""

import argparse
import math
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Collection, Optional

import requests

from artist_resolver import UNRESOLVED_SENTINEL
from cache import Cache
from config import config
from playlist_logic.scoring import (
    LASTFM_W, NOVELTY_W, RECENCY_W, SETLIST_W,
    _parse_release_date, _recency_score,
)
from sources.lastfm import LastFmClient
from sources.models import Track
from sources.setlist import (
    BASE_URL, MAX_SHOWS, SETLIST_TTL,
    _parse_setlist_date, parse_shows, setlist_frequencies,
)
from spotify_client.auth import get_spotify_client
from spotify_client.client import (
    SpotifyClient, cached_artist_name, cached_artist_tracks, deduplicate_tracks,
)
from track_names import normalize_track_name

HISTORY_PAGES = 8                   # 20 setlists a page, so up to 160 shows per artist
HISTORY_YEARS = 4                   # stop paging once a page reaches back this far
REQUEST_DELAY = 1.0                 # seconds between setlist.fm calls, as SetlistClient
RATE_LIMIT_RETRIES = 3

TARGET_WINDOW_DAYS = config.concert_window_days   # prep looks this far ahead for shows
TARGET_MAX_SHOWS = MAX_SHOWS
MIN_TARGET_SHOWS = 2                # a single show is too noisy a target
CUTOFF_SPACING_DAYS = 90            # closer cutoffs are near-duplicate examples
MIN_CANDIDATES = 5                  # discographies smaller than this rank nothing

CV_FOLDS = 5
TOP_K = 10

# A show's date and its songs, as `parse_shows` returns them.
Show = tuple[date, Collection[str]]

FULL_FEATURES = ('setlist', 'lastfm', 'recency')
FALLBACK_FEATURES = ('lastfm', 'recency')


# ── Fetch ─────────────────────────────────────────────────────────────────────

class RateLimited(Exception):
    pass


class HistoryFetcher:
    """Pages an artist's full setlist.fm history, within a request budget."""

    def __init__(self, api_key: str, cache: Cache, max_requests: int):
        self.cache = cache
        self.requests_left = max_requests
        self._headers = {'x-api-key': api_key, 'Accept': 'application/json'}

    def get_history(self, artist_name: str) -> Optional[list[Show]]:
        """Cached shows for an artist, or None if they have not been fetched."""
        cached = self.cache.get(_history_key(artist_name))
        if cached is None:
            return None
        return [(date.fromisoformat(d), songs) for d, songs in cached]

    def fetch_history(self, artist_name: str) -> list[Show]:
        mbid = self._find_mbid(artist_name)
        raw: list[dict] = []
        if mbid:
            horizon = date.today() - timedelta(days=365 * HISTORY_YEARS)
            for page in range(1, HISTORY_PAGES + 1):
                data = self._get(f'/artist/{mbid}/setlists', {'p': page})
                setlists = data.get('setlist', [])
                raw.extend(setlists)
                oldest = _parse_setlist_date(setlists[-1].get('eventDate', '')) if setlists else None
                if (not setlists
                        or page * data.get('itemsPerPage', 20) >= data.get('total', 0)
                        or (oldest and oldest < horizon)):
                    break

        shows = parse_shows(raw)
        self.cache.set(
            _history_key(artist_name),
            [(d.isoformat(), sorted(songs)) for d, songs in shows],
            SETLIST_TTL,
        )
        return shows

    def _find_mbid(self, artist_name: str) -> Optional[str]:
        """
        The MusicBrainz ID behind an artist name, found the way production
        finds setlists — by name search — then pinned to one artist, so paging
        can't wander into a namesake's history.
        """
        data = self._get('/search/setlists', {'artistName': artist_name, 'p': 1})
        wanted = artist_name.casefold()
        mbids = Counter(
            sl['artist']['mbid']
            for sl in data.get('setlist', [])
            if sl.get('artist', {}).get('name', '').casefold() == wanted
        )
        return mbids.most_common(1)[0][0] if mbids else None

    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(RATE_LIMIT_RETRIES):
            if self.requests_left <= 0:
                raise RateLimited('request budget spent')
            self.requests_left -= 1
            time.sleep(REQUEST_DELAY)
            resp = requests.get(f'{BASE_URL}{path}', headers=self._headers, params=params, timeout=15)
            if resp.status_code == 404:
                return {}   # setlist.fm's answer for "no setlists"
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get('Retry-After', 5 * 2 ** attempt)))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RateLimited(f'429 from setlist.fm {RATE_LIMIT_RETRIES} times running')


def _history_key(artist_name: str) -> str:
    return f'setlist_history:{artist_name.lower().strip()}'


def _require_keys() -> None:
    missing = [k for k, v in [
        ('SETLIST_FM_API_KEY', config.setlist_fm_api_key),
        ('LASTFM_API_KEY', config.lastfm_api_key),
    ] if not v]
    if missing:
        sys.exit(f'Training needs {", ".join(missing)} in .env')


def _connect_spotify() -> Optional[SpotifyClient]:
    """
    A client that raises on 429 rather than sleeping out Retry-After — after a
    burst of discography fetches Spotify has answered with 82,241 seconds.
    None when Spotify is unreachable, so setlist history can still be gathered.
    """
    try:
        return SpotifyClient(get_spotify_client(
            client_id=config.spotify_client_id,
            redirect_uri=config.spotify_redirect_uri,
            token_path=config.spotify_token_path,
            open_browser=False,
            status_retries=0,
        ))
    except RuntimeError as e:
        print(f'Spotify unavailable, fetching setlist history only: {e}\n')
        return None


def resolved_artist_ids(cache: Cache) -> list[str]:
    """Every artist the prep and discovery pipelines have resolved."""
    return sorted({
        v for v in cache.get_prefix('artist_resolve:').values()
        if v and v != UNRESOLVED_SENTINEL
    })


def cached_artist_pool(cache: Cache) -> dict[str, str]:
    """{id: canonical name} for resolved artists whose name is already cached."""
    names = {i: cached_artist_name(i, cache) for i in resolved_artist_ids(cache)}
    return {i: n for i, n in names.items() if n}


def cmd_fetch(max_requests: int, max_discographies: int) -> None:
    _require_keys()
    cache = Cache()
    sp = _connect_spotify()
    lastfm = LastFmClient(config.lastfm_api_key, cache)
    fetcher = HistoryFetcher(config.setlist_fm_api_key, cache, max_requests)

    ids = resolved_artist_ids(cache)
    pool = sp.get_artist_names(ids, cache) if sp else cached_artist_pool(cache)
    print(f'{len(pool)} of {len(ids)} resolved artists named; setlist.fm budget {max_requests} requests, '
          f'up to {max_discographies} new discographies\n')

    usable = 0
    spotify_failures = 0
    missing_discography: list[str] = []
    for i, (artist_id, name) in enumerate(sorted(pool.items(), key=lambda kv: kv[1].lower()), 1):
        shows = fetcher.get_history(name)
        if shows is None:
            try:
                shows = fetcher.fetch_history(name)
            except RateLimited as e:
                print(f'\nStopped: {e}. Rerun later — fetched artists stay cached for 7 days.')
                break
            except requests.RequestException as e:
                print(f'  [{i}/{len(pool)}] {name}: setlist.fm error, skipped ({e})')
                continue

        # Discographies and Last.fm cost other APIs' quota; only pay for
        # artists with enough shows to form a cutoff and a target.
        note = ''
        if len(shows) > MIN_TARGET_SHOWS:
            usable += 1
            lastfm.get_popularity_scores(name)
            tracks = cached_artist_tracks(artist_id, cache)
            if tracks is None and sp and max_discographies > 0:
                max_discographies -= 1
                tracks = sp.get_artist_tracks(artist_id, cache)
                spotify_failures = 0 if tracks else spotify_failures + 1
                if spotify_failures == 3:
                    print('  Spotify failed three artists running (rate-limited?) — no more discographies this run')
                    sp = None
            if not tracks:
                # Failures aren't cached, so a rerun retries exactly these.
                missing_discography.append(name)
                note = ' — no discography yet'
        print(f'  [{i}/{len(pool)}] {name}: {len(shows)} shows{note}')

    print(f'\n{usable} artists with enough shows to train on; '
          f'{fetcher.requests_left} setlist.fm requests left in budget')
    if missing_discography:
        print(f'{len(missing_discography)} of them still need a discography — rerun fetch later: '
              + ', '.join(missing_discography))


# ── Dataset ───────────────────────────────────────────────────────────────────

@dataclass
class Group:
    """One artist at one cutoff: a discography to rank, and what was played next."""
    artist: str
    cutoff: date
    X: list[tuple[float, ...]]  # one row per track, columns in FULL_FEATURES order
    y: list[float]              # future play frequency / its max
    has_setlist: bool


def build_groups(
    artist: str,
    shows: list[Show],
    tracks: list[Track],
    lastfm_scores: dict[str, float],
) -> list[Group]:
    shows = sorted(shows, key=lambda s: s[0])
    groups: list[Group] = []
    last_cutoff: Optional[date] = None

    for i, (cutoff, _) in enumerate(shows):
        if last_cutoff and (cutoff - last_cutoff).days < CUTOFF_SPACING_DAYS:
            continue

        window_end = cutoff + timedelta(days=TARGET_WINDOW_DAYS)
        future = [s for s in shows[i:] if s[0] < window_end][:TARGET_MAX_SHOWS]
        if len(future) < MIN_TARGET_SHOWS:
            continue

        # Production only ever sees released tracks, so neither may the model.
        candidates = [t for t in tracks if _parse_release_date(t.release_date) <= cutoff]
        if len(candidates) < MIN_CANDIDATES:
            continue

        past_freq = setlist_frequencies([s for s in shows if s[0] < cutoff])
        keys = [normalize_track_name(t.name) for t in candidates]
        X = [
            (past_freq.get(k, 0.0), lastfm_scores.get(k, 0.0), _recency_score(_parse_release_date(t.release_date), cutoff))
            for k, t in zip(keys, candidates)
        ]
        plays = [sum(k in songs for _, songs in future) for k in keys]
        if max(plays) == 0 or max(map(max, X)) == 0:
            continue   # nothing released was played, or nothing to rank on

        groups.append(Group(artist, cutoff, X, [p / max(plays) for p in plays], bool(past_freq)))
        last_cutoff = cutoff

    return groups


def load_groups(cache: Cache) -> tuple[list[Group], Counter]:
    """Examples from cached data only — fitting never calls Spotify."""
    _require_keys()
    lastfm = LastFmClient(config.lastfm_api_key, cache)
    fetcher = HistoryFetcher(config.setlist_fm_api_key, cache, max_requests=0)
    skipped: Counter = Counter()
    groups: list[Group] = []

    for artist_id, name in cached_artist_pool(cache).items():
        shows = fetcher.get_history(name)
        if shows is None:
            skipped['history not fetched (or expired)'] += 1
            continue
        if len(shows) <= MIN_TARGET_SHOWS:
            skipped['too few shows'] += 1
            continue

        lf = lastfm.get_popularity_scores(name)
        if not lf:
            skipped['no Last.fm data'] += 1
            continue
        tracks = cached_artist_tracks(artist_id, cache)
        if not tracks:
            skipped['no discography cached'] += 1
            continue
        tracks = deduplicate_tracks(tracks, lf)

        built = build_groups(name, shows, tracks, lf)
        if not built:
            skipped['no usable cutoff'] += 1
        groups.extend(built)

    return groups, skipped


# ── Model ─────────────────────────────────────────────────────────────────────
#
# Plain Python rather than numpy: there are at most three weights, and the
# project's environment doesn't have numpy.

Vector = list[float]


def project(groups: list[Group], features: tuple[str, ...]) -> list[Group]:
    """The same groups with only the named feature columns kept."""
    cols = [FULL_FEATURES.index(f) for f in features]
    return [
        Group(g.artist, g.cutoff, [tuple(x[c] for c in cols) for x in g.X], g.y, g.has_setlist)
        for g in groups
    ]


def softmax(theta: Vector) -> Vector:
    top = max(theta)
    e = [math.exp(t - top) for t in theta]
    return [v / sum(e) for v in e]


def normalized(w: Vector) -> Vector:
    return [v / sum(w) for v in w]


def _scores(w: Vector, X: list[tuple[float, ...]]) -> Vector:
    return [sum(wj * xj for wj, xj in zip(w, x)) for x in X]


def ratio_loss(w: Vector, g: Group) -> float:
    s = _scores(w, g.X)
    top = max(s)
    if top <= 0:
        return sum(yi * yi for yi in g.y) / len(g.y)
    return sum((si / top - yi) ** 2 for si, yi in zip(s, g.y)) / len(g.y)


def ratio_loss_and_grad(w: Vector, g: Group) -> tuple[float, Vector]:
    """
    Squared error between score/max-score and the target, and its gradient
    with respect to the weights. The max is piecewise — its argmax holds the
    denominator — so the gradient flows through that track's features too.
    """
    s = _scores(w, g.X)
    m = max(range(len(s)), key=s.__getitem__)
    top, xm = s[m], g.X[m]
    if top <= 0:
        return ratio_loss(w, g), [0.0] * len(w)

    loss, grad = 0.0, [0.0] * len(w)
    for x, si, yi in zip(g.X, s, g.y):
        o = si / top
        r = o - yi
        loss += r * r
        for j in range(len(w)):
            grad[j] += r * (x[j] - o * xm[j])
    n = len(g.y)
    return loss / n, [2 * v / (n * top) for v in grad]


def _log_softmax(s: Vector) -> Vector:
    top = max(s)
    lse = top + math.log(sum(math.exp(v - top) for v in s))
    return [v - lse for v in s]


def listnet_loss(w: Vector, g: Group) -> float:
    return listnet_loss_and_grad(w, g)[0]


def listnet_loss_and_grad(w: Vector, g: Group) -> tuple[float, Vector]:
    """
    Cross-entropy between the artist's actual play distribution (target over
    its sum) and a softmax over the discography's scores. A softmax is
    dominated by its largest scores, so this is decided by which songs rise
    to the top — the part of the ranking a playlist actually uses — rather
    than by how close every never-played deep cut sits to zero.
    """
    log_p = _log_softmax(_scores(w, g.X))
    total = sum(g.y)
    grad = [0.0] * len(w)
    loss = 0.0
    for x, lp, yi in zip(g.X, log_p, g.y):
        target = yi / total
        loss -= target * lp
        diff = math.exp(lp) - target
        for j in range(len(w)):
            grad[j] += diff * x[j]
    return loss, grad


@dataclass(frozen=True)
class Objective:
    """
    A loss plus how its weights are parameterised. The ratio loss cannot see
    scale, so its weights live on the simplex (softmax). ListNet's softmax
    temperature *is* the weights' scale, so it fits positive weights of any
    size (exp) and only their normalised ratios are reported.
    """
    name: str
    loss: Callable[[Vector, Group], float]
    loss_and_grad: Callable[[Vector, Group], tuple[float, Vector]]
    to_weights: Callable[[Vector], Vector]
    chain: Callable[[Vector, Vector], Vector]     # (w, dL/dw) → dL/dθ
    lr: float
    grid_step: float
    scales: tuple[float, ...]                      # magnitudes tried for fixed ratios


RATIO = Objective(
    'ratio', ratio_loss, ratio_loss_and_grad,
    to_weights=softmax,
    # dw_j/dθ_i = w_i (δij - w_j)
    chain=lambda w, g: [wi * (gi - sum(a * b for a, b in zip(w, g))) for wi, gi in zip(w, g)],
    lr=2.0, grid_step=0.02, scales=(1.0,),
)

LISTNET = Objective(
    'listnet', listnet_loss, listnet_loss_and_grad,
    to_weights=lambda theta: [math.exp(t) for t in theta],
    chain=lambda w, g: [wi * gi for wi, gi in zip(w, g)],
    lr=0.5, grid_step=0.05, scales=tuple(2.0 ** k for k in range(-1, 8)),
)

OBJECTIVES = {o.name: o for o in (RATIO, LISTNET)}


def artist_balance(groups: list[Group]) -> Vector:
    """Per-group weights so every artist counts once, however long their history."""
    per_artist = Counter(g.artist for g in groups)
    return [1 / per_artist[g.artist] for g in groups]


def _balanced_mean(values: Vector, balance: Vector) -> float:
    return sum(v * b for v, b in zip(values, balance)) / sum(balance)


def dataset_loss(w: Vector, groups: list[Group], obj: Objective) -> float:
    return _balanced_mean([obj.loss(w, g) for g in groups], artist_balance(groups))


def best_scale(w: Vector, groups: list[Group], obj: Objective) -> Vector:
    """`w` rescaled to whichever of the objective's magnitudes fits best."""
    unit = normalized(w)
    return min(([s * v for v in unit] for s in obj.scales), key=lambda sw: dataset_loss(sw, groups, obj))


def fit_sgd(
    groups: list[Group], obj: Objective = RATIO,
    epochs: int = 100, batch: int = 16, seed: int = 0,
) -> Vector:
    """Minibatch SGD over artist-cutoff groups; returns the fitted weights."""
    rng = random.Random(seed)
    balance = artist_balance(groups)
    k = len(groups[0].X[0])
    theta = [0.0] * k

    for epoch in range(epochs):
        step = obj.lr / (1 + epoch / 20)
        order = list(range(len(groups)))
        rng.shuffle(order)
        for start in range(0, len(order), batch):
            idx = order[start:start + batch]
            w = obj.to_weights(theta)
            total = sum(balance[i] for i in idx)
            grad_w = [0.0] * k
            for i in idx:
                _, g = obj.loss_and_grad(w, groups[i])
                for j in range(k):
                    grad_w[j] += balance[i] * g[j] / total
            theta = [t - step * d for t, d in zip(theta, obj.chain(w, grad_w))]

    return obj.to_weights(theta)


def simplex_grid(k: int, step: float) -> list[Vector]:
    n = round(1 / step)
    if k == 2:
        return [[i / n, 1 - i / n] for i in range(n + 1)]
    return [[i / n, j / n, (n - i - j) / n] for i in range(n + 1) for j in range(n + 1 - i)]


def fit_grid(groups: list[Group], obj: Objective = RATIO) -> Vector:
    """
    Exhaustive search over weight ratios (and, for ListNet, magnitudes). It
    cannot land in a local minimum, so it checks SGD — to within the grid step.
    """
    candidates = [
        [s * v for v in w]
        for w in simplex_grid(len(groups[0].X[0]), obj.grid_step)
        for s in obj.scales
    ]
    return min(candidates, key=lambda w: dataset_loss(w, groups, obj))


# ── Evaluation ────────────────────────────────────────────────────────────────

def ranking_metrics(w: Vector, groups: list[Group], obj: Objective, seed: int = 0) -> dict[str, float]:
    """
    Artist-balanced precision@K (share of the top K that were played) and
    NDCG@K (graded by play frequency). Ties are broken at random: most tracks
    score identically on sparse signals, and list order would otherwise decide.
    """
    rng = random.Random(seed)
    prec, ndcg = [], []
    for g in groups:
        s = _scores(w, g.X)
        jitter = [rng.random() for _ in s]
        top = sorted(range(len(s)), key=lambda i: (-s[i], jitter[i]))[:TOP_K]
        discounts = [1 / math.log2(r + 2) for r in range(len(top))]
        ideal = sum(y * d for y, d in zip(sorted(g.y, reverse=True), discounts))
        prec.append(sum(g.y[i] > 0 for i in top) / len(top))
        ndcg.append(sum(g.y[i] * d for i, d in zip(top, discounts)) / ideal)

    balance = artist_balance(groups)
    return {
        'loss': dataset_loss(w, groups, obj),
        f'p@{TOP_K}': _balanced_mean(prec, balance),
        f'ndcg@{TOP_K}': _balanced_mean(ndcg, balance),
    }


def artist_folds(groups: list[Group], n: int, seed: int = 0) -> list[list[Group]]:
    """Split by artist, so no held-out artist was seen in training."""
    artists = sorted({g.artist for g in groups})
    random.Random(seed).shuffle(artists)
    fold_of = {a: i % n for i, a in enumerate(artists)}
    folds: list[list[Group]] = [[] for _ in range(n)]
    for g in groups:
        folds[fold_of[g.artist]].append(g)
    return folds


def _fmt(features: tuple[str, ...], w: Vector, places: int = 3) -> str:
    return '  '.join(f'{f}={v:.{places}f}' for f, v in zip(features, normalized(w)))


def evaluate(
    label: str, features: tuple[str, ...], groups: list[Group], current: Vector, obj: Objective,
) -> Vector:
    groups = project(groups, features)
    n_artists = len({g.artist for g in groups})
    print(f'\n━━ {label}: {len(groups)} examples, {n_artists} artists ━━')
    if n_artists < CV_FOLDS:
        print('  Too few artists to cross-validate.')
        return current

    baselines = {'current': current}
    for i, f in enumerate(features):
        baselines[f'{f} only'] = [float(j == i) for j in range(len(features))]

    results: dict[str, list[dict]] = defaultdict(list)
    fold_weights = []
    folds = artist_folds(groups, CV_FOLDS)
    for i, test in enumerate(folds):
        train = [g for j, fold in enumerate(folds) if j != i for g in fold]
        w = fit_sgd(train, obj)
        fold_weights.append(normalized(w))
        results['learned'].append(ranking_metrics(w, test, obj))
        for name, bw in baselines.items():
            # Baselines are only ratios; give each its best magnitude on the
            # test fold. That flatters them, which errs against the learned fit.
            results[name].append(ranking_metrics(best_scale(bw, test, obj), test, obj))

    print(f'\n  Held-out artists, mean over {CV_FOLDS} folds ({obj.name} loss: lower is better):')
    print(f'  {"":<16}{"loss":>8}{f"p@{TOP_K}":>8}{f"ndcg@{TOP_K}":>9}')
    for name, rows in results.items():
        m = {k: sum(r[k] for r in rows) / len(rows) for k in rows[0]}
        print(f'  {name:<16}{m["loss"]:>8.4f}{m[f"p@{TOP_K}"]:>8.3f}{m[f"ndcg@{TOP_K}"]:>9.3f}')

    print('\n  Learned weights per fold (the spread shows how stable they are):')
    for j, f in enumerate(features):
        col = [w[j] for w in fold_weights]
        print(f'    {f:<8} ' + '  '.join(f'{v:.3f}' for v in col) + f'   (sd {statistics.pstdev(col):.3f})')

    w_sgd = fit_sgd(groups, obj)
    w_grid = fit_grid(groups, obj)
    w_cur = best_scale(current, groups, obj)
    print(f'\n  All data (SGD): {_fmt(features, w_sgd)}   loss {dataset_loss(w_sgd, groups, obj):.4f}')
    print(f'  Grid check:     {_fmt(features, w_grid, 2)}   loss {dataset_loss(w_grid, groups, obj):.4f}')
    print(f'  Current:        {_fmt(features, w_cur)}   loss {dataset_loss(w_cur, groups, obj):.4f}')
    return normalized(w_sgd)


def cmd_fit(obj: Objective) -> None:
    groups, skipped = load_groups(Cache())
    if skipped:
        print('Artists left out: ' + ', '.join(f'{n} {why}' for why, n in skipped.most_common()))
    if not groups:
        sys.exit('No training examples — run `python train_weights.py fetch` first.')

    signal_w = 1 - NOVELTY_W
    current_full = normalized([SETLIST_W, LASTFM_W, RECENCY_W])
    current_fallback = normalized([LASTFM_W, RECENCY_W])

    w_full = evaluate('With setlist data', FULL_FEATURES,
                      [g for g in groups if g.has_setlist], current_full, obj)
    w_fb = evaluate('Without setlist data (setlist hidden)', FALLBACK_FEATURES,
                    groups, current_fallback, obj)

    print(f'\n━━ Suggested constants (sharing {signal_w:.2f} with NOVELTY_W = {NOVELTY_W}) ━━\n')
    for f, v in zip(FULL_FEATURES, w_full):
        print(f'  {f.upper() + "_W":<11} = {signal_w * v:.2f}')

    # score_track has one set of constants for both regimes, so the fallback
    # ratio is whatever the full model's Last.fm : recency ratio happens to be.
    implied = w_full[1] / (w_full[1] + w_full[2])
    print(f'\n  Without setlist data those give Last.fm {implied:.2f} : recency {1 - implied:.2f};')
    print(f'  fitting that regime directly gives {w_fb[0]:.2f} : {w_fb[1]:.2f}.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch', help='gather setlist history, discographies and Last.fm data')
    fetch.add_argument('--max-requests', type=int, default=1000,
                       help='setlist.fm request budget for this run (default 1000)')
    fetch.add_argument('--max-discographies', type=int, default=15,
                       help='new Spotify discographies to download this run, ~27 requests each (default 15)')
    fit = sub.add_parser('fit', help='fit and evaluate weights from fetched data')
    fit.add_argument('--loss', choices=sorted(OBJECTIVES), default='ratio',
                     help='ratio: squared error of score/max (default); listnet: ranking cross-entropy')
    args = parser.parse_args()

    if args.command == 'fetch':
        cmd_fetch(args.max_requests, args.max_discographies)
    else:
        cmd_fit(OBJECTIVES[args.loss])


if __name__ == '__main__':
    main()
