# SEO change log

Every page change, with the date and the numbers before it, so each fix is judged by
results (Search Console position and clicks, 4-6 weeks later), not opinion.
Baselines come from Governor (`scripts/line_demand.py`, `scripts/opportunity_audit.py`).

| # | Date | Page | Change | Target searches (volume/mo) | Baseline position / clicks | Check on |
|---|------|------|--------|-----------------------------|----------------------------|----------|
| 0 | 2026-11-09 | /montessori-toys.html | "Where to Start" carousel (8 products, server-rendered) | montessori toys (33,100) | #14-16.6, 13-19 clicks/90d | 2026-12-21 |
| 1 | 2026-11-10 | /wooden-name-puzzles.html | 12 name puzzle stools added (3 -> 19 products); meta title "Personalized Name Puzzles for Kids \| Wooden, Made in USA"; new meta description | name puzzle (2,400); wooden name puzzles (1,000); personalized puzzle (5,400) | #16 (via /nursery-decor.html); #6; #25 | 2026-12-22 |
| 1b | 2026-11-10 | /nursery-decor.html | link "personalized name puzzles" -> /wooden-name-puzzles.html | (supports #1) | | 2026-12-22 |
| 2 | 2026-11-10 | /personalized-books-for-kids.html | meta title "Personalized Books for Kids \| Your Child's Name in the Story" (was "Personalized Storybooks..."); new meta description; sort Name -> Position with Christmas/ornament books first | personalized books for kids (8,100); personalized book (8,100) | #9 with 1 click; #30 | 2026-12-22 |
| 3 | 2026-11-10 | /personalized-step-stools.html | meta title "Name Puzzle Stools \| Personalized Step Stools for Kids, USA" (was "...Create and Play"); new meta description; sort Name -> Position, Heirloom stool first | name puzzle stool (590); personalized step stools (2,900) | #11 (3 clicks); not ranking | 2026-12-22 |

Parked (on-page already fine, problem is assortment/competition): /personalized-baby-blankets.html,
personalized baby blankets 22,200/mo at #54 (11 I See Me designs vs Etsy/Amazon/Personalization Mall).

## Site-wide changes (affect all measurements)
- 2026-11-09: Consent Mode defaults granted outside EU/UK/CH (consent.phtml). Expect more
  Google Ads conversions and fewer GA4 "(not set)" orders from this date.
- 2026-11-09: RECOMMENDED pausing Google Ads campaigns "Name Trains Search-2026" and
  "PMAX - Carpets for Kids" (0 ROAS over Oct 1 - Nov 9); not yet confirmed done.
  Re-evaluate the two remaining PMAX campaigns around 2026-11-30.
