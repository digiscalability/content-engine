# ADR 0002: AI disclosure and post status through Postiz, per channel

- Status: accepted, 2026-11-13 (slice P0.6); to be confirmed by the first live draft (P0.9)
- Requirements: FR-PUB-2, FR-PUB-3, FR-PUB-4, GR-5, §10, C15

## Context

FR-PUB-3 says AI disclosure is set through Postiz where Postiz exposes it, and asks P0.6 to verify which route each channel needs. §10 asks whether the Postiz CLI can list posts with their status, or whether reconciliation needs the public API. No Postiz account exists yet (G0.7), so the evidence is Postiz's own source code (`gitroomhq/postiz-app` at commit `4823080b4135`, 12 Nov 2026), its CLI package (`postiz@2.0.27`, `gitroomhq/postiz-agent`) and the vendors' docs.

## Findings

| Channel | What Postiz exposes | Evidence |
|---|---|---|
| YouTube | `title`, `type` (public, private, unlisted), `selfDeclaredMadeForKids`, `thumbnail`, `tags`. **No synthetic-content setting.** The upload sends only `privacyStatus` and `selfDeclaredMadeForKids` in `status`, never YouTube's `containsSyntheticMedia`. | `youtube.settings.dto.ts`, `youtube.provider.ts` |
| TikTok (Phase 1) | `video_made_with_ai`, sent as TikTok's `is_aigc`: video posts only, and only with `content_posting_method: "DIRECT_POST"` (dropped on `UPLOAD`). | `tiktok.dto.ts`, `tiktok.provider.ts` |
| Instagram (Phase 1) | No AI setting (`post_type`, trial reels, collaborators, audio). | `instagram.dto.ts` |
| Status | `posts:list` calls `GET /public/v1/posts` and returns `{posts: [...]}` with `id`, `publishDate`, `releaseURL`, `releaseId`, `state` (QUEUE, PUBLISHED, ERROR, DRAFT) and `error`; deleted posts are left out. | `posts.repository.ts`, `schema.prisma`, CLI `dist/index.js` |

## Decision

1. **YouTube: the private route (C15).** Every avatar post is drafted with `type: "private"` (`youtube_settings(..., ai_disclosure=True)`). Abbas schedules the draft in Postiz, which uploads it as private; he then ticks "altered or synthetic content" in YouTube Studio and makes it public or schedules it there. Postiz reports the upload as `PUBLISHED`; for a private-route post the engine records `uploaded_private` (§10), and the digest lists the label tick. Postiz cannot see YouTube visibility, so the move to `published` needs Abbas's word or a YouTube Data API read; P1.7 (metrics and reconciliation) picks one.
2. **TikTok: through Postiz.** `tiktok_settings()` always sends `DIRECT_POST` and `video_made_with_ai`. No manual step.
3. **Instagram: not for avatar posts yet.** Postiz has no AI field and Instagram's API has no draft or private upload, so under FR-PUB-3 Instagram carries no avatar posts until a route exists. P1.5 revisits this (for example, Abbas adding Meta's AI label in the app).
4. **Status: the CLI is enough.** Reconciliation reads `posts:list`; no public-API fallback is needed. `engine_state()` maps DRAFT → `drafted`, QUEUE → `scheduled`, PUBLISHED → `published` (or `uploaded_private` on the YouTube route), ERROR → `failed`, and a post missing from the list → `rejected` (Abbas deleted it).
5. **Settings Postiz would drop are refused.** `draft_post()` reads `integrations:settings` first and stops when a setting is not in the channel's schema (FR-PUB-2), so, for example, a `containsSyntheticMedia` flag can never be "sent" and silently lost.

## To confirm on the first live draft (P0.9)

- The `posts:create` response carries the post id as `postId` (the adapter also accepts `id`).
- `integrations:settings` puts the schema at `output.settings.properties`.
- A private YouTube upload shows as `PUBLISHED` with a `releaseURL`.

If any differ, P0.9 updates the adapter and this ADR.
