# ADR 0023: Stock fetch and the music pool

- Status: accepted, 2026-11-14 (slice P0.6b.2)
- Requirements: SRD FR-ING-5, FR-ING-6, FR-SRC-2, FR-SRC-3, FR-PRD-4, §5.4 `search_library`, §8.1, §8.3, §11, §13 (02:00 ingest), C14, GR-7, NFR-1, NFR-5, NFR-7, NFR-8, NFR-9

## Context

P0.6b.2 adds the two Phase 0 library inputs that come from outside Abbas's own folders: Pexels stock (FR-ING-5) and a YouTube Audio Library music pool (FR-ING-6). There is no `PEXELS_API_KEY` yet, so everything runs on a fake. The SRD leaves open:

- how "fetches happen only for beats the scriptwriter asked for" works, when §5.4 gives the scriptwriter no fetch tool and says ingest runs as Python jobs;
- where a per-clip licence record lives, and what "not searchable without one" checks;
- which Pexels files to take, and how the daily cap counts;
- how tracks enter, what is known about each, and how a scriptwriter finds one.

## Decision

1. **Two sources written out in `sources.yaml`, off until Abbas says yes.**
   - `pexels`: `kind: library` with the API `url`, tier `stock`, the Pexels licence URL, attribution `Pexels`.
   - `youtube-audio-library`: `kind: library` with `path: media/library/music` (G0.9b), tier `stock`, YouTube's Audio Library help page as licence URL, attribution empty.
   - The SRD names both (C14, FR-ING-5, FR-ING-6), but FR-SRC-3 and the P0.6b.1 guard keep the live list to what Abbas named. So both sit commented out under "READY FOR P0.6b.3", and a test checks they validate once uncommented. Stock and music stay off until Abbas says yes in P0.6b.3, with G0.9a and G0.9b.
   - A library source now needs a path (a local pool) or a url (an API). `engine.yaml` `stock.source` names the stock API; a missing source switches stock off, and a source of another kind is a config error.
   - A `stock`, `cc_by` or `partner_written` source now needs an `attribution` (`""` when the licence asks for none). The schema already required one on its assets, so a source without it would have stopped the nightly import; now config validation names it (ADR 0007's pattern).
2. **Requests come from `search_library`, the fetch is a nightly job.**
   - `search_library` takes an optional `brief_id`. A non-empty footage query with a brief is queued in `stock_requests` when it finds nothing, or when its best scene scores under `stock.thin_below` by CLIP. One request per brief and query.
   - The only caller of a stock provider is `ce stock fetch`, which reads that queue. No CLI or tool fetches an arbitrary query, so stock follows the scriptwriter's beats by construction.
   - The scriptwriter prompt asks for one search per beat with the brief id. Today's post uses the best clip it has; the fetched clips serve later posts.
   - `run_ingest.ps1` (02:00) runs `ce stock fetch`, then `ce ingest`, and exits with the first failing code. Without the key and with requests waiting, the fetch exits 2 naming G0.9a.
3. **`thin_below` is 0.23, measured.** On the drawn fixtures the real CLIP scores the five sample beats 0.261–0.306 and five beats the library cannot show 0.141–0.205. 0.23 sits in the gap, and `test_thin_below_separates_matches_from_misses` (`-m model`) pins it. Real footage will score differently; P0.6b.3 retunes it on Abbas's clips.
4. **The fetch.**
   - Requests are worked oldest first, skipping identities with the kill flag (GR-7).
   - Up to `per_request` (2) clips a request and `daily_cap` (10) a local day, counted from the licence records' `fetched_on`.
   - A clip is taken only if it has a portrait MP4 at least 1080 wide (the narrowest such file, so never 4K and never upscaled) and runs at most `max_clip_s` (30 s). A Pexels id is never fetched twice.
   - A rate limit (429) or the disk stop line ends the run and leaves the rest pending; any other provider error fails that request only, with its text.
   - Clips land in `media/library/stock/<source>/<provider>-<id>.mp4`. `ce ingest` treats that folder as a source root and indexes the clips like own footage: scenes, CLIP vectors and the people check (§8.3 applies to stock too).
5. **One licence record per library asset** (`asset_licences`, migration 0006).
   - It holds the licence URL, the terms (the source's rights proof), the credit, and for stock the creator, creator page, clip page, provider id and fetch day.
   - A fetched clip's asset, path and licence record are written in one transaction, so no fetched clip exists without one.
   - Search, for footage and music, and `rights_violations` (so `submit_edit_plan`, H4 and FR-QA-1) accept an asset from a library source only with its record. This is FR-ING-5's accept line, and a file dropped by hand into the stock folder is indexed but never offered.
   - The asset row keeps the source-level licence and credit that ingest refreshes; the per-clip credit (`Video by <name> on Pexels`, Pexels' guideline form) lives in the record.
6. **The Pexels adapter** (`engine/library/stock.py`) calls `GET /v1/videos/search` (`orientation=portrait`, `size=medium`) with the key in the `Authorization` header of the API client only.
   - File downloads go to Pexels' file host without the key, over https only, written to `.part`, checked for an MP4 `ftyp` box, then moved into place.
   - `FakeStock` serves a fixture catalogue. The contract tests run the same assertions on both, and the real search is `tests/contract/test_stock_live.py` under `live`.
7. **The music pool** (`engine/library/music.py`) is imported by `ce ingest` before the footage, with no model needed.
   - Library sources with a path are scanned for audio files (`.mp3 .m4a .aac .wav .flac .ogg .opus`), hashed and deduped like footage, and read in place. Each becomes an `audio` asset with people `none`.
   - Each track gets a `tracks` row with title and artist from its tags (else the file name), its tempo from the first 90 s by the style analyser's onset tempo, and an energy band. `energy_band()` in `engine/style/look.py` is now the one rule looks and tracks share.
   - Its licence record carries the source's terms. Each scan copies the source's licence onto its tracks again.
   - A file whose bytes already belong to another source (the engine's voice WAVs, say) is skipped. A failed file is recorded and waits for `--retry-failed`.
8. **`search_library` gains `kind: "music"`**, with optional `energy` and `tempo_bpm` from the look.
   - It returns imported tracks only (never voice WAVs), filtered by tier, expiry and licence record.
   - Tracks are ordered by the energy asked, then distance outside the tempo range, then query words in the title or artist, each with its credit. Footage results never include audio.

## Consequences

- The done-when holds on fakes: the Pexels contract tests pass on the fake and on Pexels over a mocked transport, and imported tracks come back from `search_library`.
- P0.6b.3 needs Abbas's yes to the two sources, G0.9a (the key) for one real fetch, and G0.9b (10–20 tracks in `media/library/music`, "Attribution not required").
- **Open for Abbas before TikTok or Instagram posts (P1.5):** YouTube's Audio Library page says the tracks are copyright-safe for YouTube. It does not say whether they may be used on other platforms, and it gives no guidance for use off YouTube. Today every render goes to every channel of its identity, so a pool track would reach TikTok and Instagram once those channels are on.
- A Creative Commons track needs its credit in the video description; it should sit in its own source with its credit as attribution. Showing credits in post captions is not built yet: the licence record holds them for the slice that writes captions (P1.5.1).
- Not built: a digest line for waiting stock requests (`ce stock fetch --dry-run` lists them), Pexels photos, other stock providers, and tempo for tracks whose beat the onset method cannot find (their tempo stays empty and they sort last when a range is asked).
