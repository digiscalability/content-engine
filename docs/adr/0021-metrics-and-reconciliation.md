# ADR 0021: Metrics pull and reconciliation

- Status: accepted, 2026-11-13 (slice P1.7); to be confirmed by the first live post (P0.9)
- Requirements: FR-LRN-1, SRD §10 (states, step 3, `ce reject`), FR-PUB-3, §11 `metrics_snapshots`, §13, NFR-1, NFR-8; settles ADR 0002 decision 1

## Context

FR-LRN-1 pulls metrics at 24 h, 72 h and 7 d after publish with `postiz analytics:post`, and resolves `{"missing": true}` with `posts:missing` / `posts:connect`. §10 has `ce reconcile` follow each draft through Postiz. ADR 0002 left one choice to this slice: how a private-route YouTube post moves from `uploaded_private` to `published`, which Postiz cannot see.

No Postiz account exists yet (G0.7), so the evidence is Postiz's source:
- the CLI `gitroomhq/postiz-agent` at 2.0.27 (the pinned version);
- the server `gitroomhq/postiz-app` at `23f29c4` (13 Nov 2026).

What it shows:
- `analytics:post <id> -d <days>` calls `GET /public/v1/analytics/post/:id`. It returns `[{label, percentageChange, data: [{total, date}]}]`, or `{"missing": true}` when the post's `releaseId` is `missing`, or `[]` when nothing is available (no release id yet, an error, or a provider without post analytics).
- YouTube's `postAnalytics` reads `videos.list` statistics: Views, Likes, Comments, Favorites. TikTok's reads `video/query`: Views, Likes, Comments, Shares. Each is one data point holding the lifetime total today. Instagram's labels are the same kind.
- `posts:missing` lists the channel's 20 newest videos as `{id, url}` (a cover image) only while the post's `releaseId` is `missing`. `posts:connect` sets it.
- `posts:list` (`GET /public/v1/posts`) has no maximum window. It leaves out deleted posts and posts of a deleted channel.

## Decision

1. **`uploaded_private → published` by YouTube's oEmbed endpoint** (`https://www.youtube.com/oembed?url=…&format=json`). It needs no key, account or quota.
   - A 200 means anyone can watch the video. 400, 401, 403 and 404 mean not public. Checked by hand: a public video answers 200 and an unknown id 400; YouTube does not document the private case, so it waits for P0.9.
   - Anything else, or a network error, is a warning and the post keeps waiting. A post is never marked live on a guess.
   - `published_at` is the first reconcile run that sees it public: at most 6 h late, the job's interval.
   - Not chosen: Abbas's word (another daily step for him), or the YouTube Data API (a key, `YOUTUBE_DATA_API_KEY`, that the SRD schedules for Phase 2). When that key arrives, `videos.list part=status` can replace oEmbed behind the same `Visibility` interface.
   - Unlisted videos also answer 200. The flow tells Abbas to make the video public or schedule it, so unlisted is not expected.
   - oEmbed also refuses a public video whose embedding is turned off ("Allow embedding" in Studio, on by default). Such a post would stay `uploaded_private`, and the digest would keep listing its label tick, which is the visible sign. Leave embedding on.
2. **One snapshot per run: the latest slot that has come due.** Postiz returns lifetime totals as of the pull. A post first reached at day 8 (the PC was off) gets a `7d` row, not three copies of the same numbers under `24h`, `72h` and `7d`. `taken_at` records when the numbers were read.
   - Posts are pulled until 14 days after publish.
   - `-d` is the slot's days (1, 3, 7); YouTube and TikTok ignore it.
   - A Postiz error on one post is reported and the run goes on to the others (each row commits on its own); `ce metrics` then exits 1 so the job log shows it. Found by the automated review: without this, one broken post blocked every later post for up to 14 days.
3. **Columns: views, likes, comments, shares.** Postiz has no source for `watch_time`, `followers_delta` or `clicks` per post: links skip its shortener so their UTM tags stay. They stay NULL until a richer source exists (YouTube Analytics, Phase 3). A pull with none of the four writes nothing and is retried.
4. **Missing release ids are connected only when the match is certain.**
   - The candidates are the videos from `posts:missing` that no post has already claimed.
   - They are matched newest to newest with the channel's published posts still `missing`, and only when the two counts are equal.
   - A video posted outside the engine makes the counts differ. The post is then left alone, and the digest asks Abbas to connect it in the Postiz app. Reconcile picks up a release id that Abbas connects there.
5. **Reconcile never rejects on a partial view.**
   - A post missing from `posts:list` becomes `rejected` only if Postiz listed at least one post for its channel. A disconnected channel lists none, so its posts stay put and the run warns.
   - A post rejected by reconcile (no reason) comes back if it reappears within 30 days. A reason from `ce reject` is final.
   - Published and private-uploaded posts never move back. Postiz may only fill in their platform id.
   - All Postiz-driven changes are written in one transaction after the list is read, so a failed call changes nothing.
   - The list window runs from 30 days before the oldest open post to 365 days ahead.
6. **Ids from Postiz are checked before they go back on its command line.** `npx` is a `.cmd` shim, so cmd.exe parses the arguments, and post and release ids now come from Postiz and platform answers. `safe_id()` allows only letters, digits, `_` and `-` (1–64), which covers Postiz cuids and YouTube and TikTok ids. Anything else is refused before a process starts, and `posts:missing` candidates with other ids are dropped (automated security review).
7. **`ce reject <post_id> "<why>"`** works on drafted, scheduled, private-uploaded and rejected posts. The engine deletes nothing, so the command reminds Abbas to delete the draft in Postiz, or the private video in Studio.
8. **`--dry-run`.** For `ce reconcile` and `ce metrics` it calls and writes nothing; it lists the posts or snapshots a live run would handle. A fake Postiz has none of the real posts, so a faked run against the real database would reject every draft. The whole loop runs on fakes inside `ce daily --dry-run` instead: fake Abbas schedules, publishes and makes public each draft, then the real reconcile and metrics jobs take it to `published` with a 24 h snapshot in the run's own DB.
9. **Schedule.** `run_daily.ps1` runs daily, reconcile, then the digest. `run_metrics.ps1` runs reconcile, then metrics, every 6 h. The existing `ContentEngine-Metrics` entry now has its wrapper; registration stays a dry run until Abbas says yes.

## To confirm on the first live post (P0.9)

- A private YouTube video's oEmbed answer is one of 400, 401, 403 or 404.
- `posts:list` carries `releaseId` (the YouTube video id) for a private upload.
- `analytics:post` for a YouTube post returns the labels above.

If any differ, P0.9 updates `engine/publish/` and this ADR.

## Consequences

- Phase 3's retro (FR-LRN-2) and hit rate (FR-LRN-4) read `metrics_snapshots`. They should compare posts at the same slot, and may discount a snapshot whose `taken_at` is well past its slot.
- GR-7's automatic reach-drop kill (Phase 5) has its input here.
