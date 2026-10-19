# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The operator and one or two invited friends, all job seekers. Each person opens their own invite link, drops a resume once, and from then on checks back to see what was found. Use is split about evenly between desktop and phone: on desktop they sweep the list, open promising postings, and knock out the rest; on a phone they glance at what is new.

## Product Purpose

Shortlist finds LinkedIn jobs on its own and keeps finding them. From one resume it builds a search profile, polls LinkedIn on a schedule the user picks (30 minutes to 8 hours), adds every new posting to the user's list the moment it is found, and has Jev score how well each one fits. Success is a user who no longer searches by hand: they come back to a list that grew while they were away and only has to be trimmed.

## Positioning

The search runs without the user. Collection is the product; scoring is the help on top. A job board waits to be searched; Shortlist searches, logged out and slowly, in the background, and the user only reviews.

## Operating Context

- Invite-only web app (`jjh serve`): each user reaches it through a private invite link; a signed cookie keeps them in for 90 days. Users never see each other's data.
- Collection keeps running on the server after the page is closed. Search runs every 30–60 min by default (user-selectable up to 6–8 h), pauses 01:00–07:00, and uses random gaps because LinkedIn blocks bursts.
- Jev reads job titles first and picks the ones worth opening; postings are then read and scored 5 at a time about every 12 minutes. Unscored jobs show when they will be scored, or why they won't be.
- Results export to Excel. Users remove jobs they don't want (with undo); removed jobs never come back.

## Capabilities and Constraints

- Resume PDF → profile via Claude (one call per upload); name and contact details are never stored.
- Profile: headline, summary, years, current level, target levels (also LinkedIn's experience filter), skills, target roles, locations, languages, up to 3 search terms, search location, work type.
- LinkedIn is read logged out over plain HTTP; company apply links are mostly hidden, so most postings are scored from LinkedIn's own description.
- "Search now" is held for 10 minutes after a search so searches never bunch up.
- Every score is Jev's (TypeSafe System One); the app never overrides a score with its own rules. Overall fit is Jev's judgment on whether to apply, not an average of the parts.
- Hosting not decided yet (Fly.io or Oracle Cloud); runs locally today.

## Brand Commitments

- The name is **Shortlist** (formerly JevScout).
- Korean and English, switchable in the page; every string exists in both.
- Automatic collection is the headline: "it finds jobs for you" comes before "Jev scores them".

## Evidence on Hand

- Real collection data from the operator's own runs, under `data/users/` — private, never published.
- No testimonials, user counts, or success metrics exist; do not invent any.

## Product Principles

1. The list grows without the user: show what was found and when the next search runs before anything else.
2. Never leave a job unexplained: every row says its score, when it will be scored, or why it won't be.
3. Slow on purpose: pacing protects the user's access to LinkedIn; the interface explains waits instead of hiding them.
4. Trimming is the main job: removing, filtering and opening must be fast on desktop and on a phone.
5. Honest scores: show Jev's parts and its reasoning limits (scored from LinkedIn's description, not the company page).
