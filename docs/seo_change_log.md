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

| 4 | 2026-11-10 | /wooden-blocks.html | 17 Guidecraft products restocked (wrongly zeroed by an earlier partial-inventory qty file), reindexed: 9 -> 26 products visible; meta title "Wooden Blocks for Kids \| Unit, Rainbow & Building Block Sets" (was "...FREE Shipping" + emojis); new meta description; H1 "Wooden Blocks for Kids: Unit, Rainbow and Building Block Sets" | wooden blocks (18,100); childrens wooden blocks (6,600); wooden unit blocks (260) | #15; #15-19; #12 | 2026-12-22 |

| 5 | 2026-11-10 | /kids-tables.html, /classroom-furniture.html | Retargeted from consumer "kids table" (mass-market, CPC $0.75-0.90) to institutional searches: titles "Classroom & Preschool Tables \| Daycare and Kids Tables" and "Classroom Furniture \| Preschool and Daycare Furniture", new descriptions and H1s; 13 Whitney Brothers Top-50 Ground best sellers re-enabled (28-33% net); Kids Tables 23 -> 27 products | classroom furniture (2,900, CPC $6.55); classroom tables (1,900); daycare furniture (880); preschool tables (720); preschool furniture (590); daycare tables (480) | not ranking for any | 2026-12-22 |
| 6 | 2026-11-10 | blog posts 75, 54, 57, 329, 62 | Contextual links added: -> /classroom-rugs.html (75, 54, 57), -> /classroom-furniture.html (75, 329, 62), -> /kids-tables.html (329). Before: 0 of 101 posts linked to classroom furniture or kids tables | (supports #5 and classroom rugs) | | 2026-12-22 |

| 7 | 2026-11-10 | /rainbow-seating-rug-8x13.html (SKU 8434) | Stop it competing with the category for "classroom rugs": meta title "Rainbow Seating Rug 8ft 4in x 13ft 4in \| Carpets for Kids" (was "...Classroom Kids Carpet"), H1 "Rainbow Seating Rug, 8ft 4in x 13ft 4in", link "classroom rugs" -> /classroom-rugs.html | classroom rugs (14,800) | product #17, category #28 | 2026-12-22 |

Pending: /classroom-rugs.html meta title "Classroom Rugs & Carpets for Teachers \| Circle Time Rugs" not yet
live (store-view copy). Rug best sellers for sort order: waiting on scripts/sales_by_line.py --match rug.
Disavow file (32 spam domains) generated at data/disavow.txt on the server; upload pending.

Pending: Classroom Furniture "Classroom Favorites" carousel (Fix #5b) not yet in the alphabettrains
store-view copy of the category description.

Pending: 11 Guidecraft products (unit blocks 84/135/218 pc and others) on guidecraft.com but not on the
2027 dealer price list; Frank asking Guidecraft. Restock file: docs/imports/guidecraft_restock_ask_guidecraft_first.csv.

Parked (on-page already fine, problem is assortment/competition): /personalized-baby-blankets.html,
personalized baby blankets 22,200/mo at #54 (11 I See Me designs vs Etsy/Amazon/Personalization Mall).

## Site-wide changes (affect all measurements)
- 2026-11-09: Consent Mode defaults granted outside EU/UK/CH (consent.phtml). Expect more
  Google Ads conversions and fewer GA4 "(not set)" orders from this date.
- 2026-11-09: RECOMMENDED pausing Google Ads campaigns "Name Trains Search-2026" and
  "PMAX - Carpets for Kids" (0 ROAS over Oct 1 - Nov 9); not yet confirmed done.
  Re-evaluate the two remaining PMAX campaigns around 2026-11-30.
