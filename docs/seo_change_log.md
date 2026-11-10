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
| 8 | 2026-11-10 | /name-trains.html + 3-10 letter product pages | Category meta title "Personalized Wooden Name Trains Made in the USA" (was truncated "Alphabet Name Train Toys Personalized Wooden Letter ..."), new meta description; product meta titles "Personalized {N} Letter Wooden Name Train Made in the USA" (plain, no separators, per Frank: separator titles get rewritten) replacing "Buy ... at Discount" / "on Sale" (docs/imports/name_train_meta_update.csv, store view alphabettrains) | name train / name trains; wooden name train | #7 / #7, 2,791 impr/mo, 23 clicks/mo (0.8% CTR vs ~3% typical at #7); #4 | 2026-12-22 |

7b (2026-11-10): /classroom-rugs.html meta title now "Classroom Rugs & Carpets for Teachers | Circle Time Rugs" (live).
Rug best sellers for sort order: waiting on scripts/sales_by_line.py --match rug.
Disavow: not uploaded by decision (Google de-emphasizes disavow; only needed with a manual action).

5b (2026-11-10): /classroom-furniture.html "Classroom Favorites: Whitney Brothers 2026 Best Sellers"
carousel live (8 products, server-rendered, verified).

Pending: 11 Guidecraft products (unit blocks 84/135/218 pc and others) on guidecraft.com but not on the
2027 dealer price list; Frank asking Guidecraft. Restock file: docs/imports/guidecraft_restock_ask_guidecraft_first.csv.

Parked (on-page already fine, problem is assortment/competition): /personalized-baby-blankets.html,
personalized baby blankets 22,200/mo at #54 (11 I See Me designs vs Etsy/Amazon/Personalization Mall).

## Site-wide changes (affect all measurements)
- 2026-11-10: TAG Toys closed (no longer accepting orders). All 68 TAG SKUs to be set out of stock
  (docs/imports/tag_toys_closed_out_of_stock.csv), kept online for rankings/backlinks; Montessori Toys
  carousel SC2/SM11/RH10 replaced by Guidecraft G5066/G5084/G3082. Expect fewer products on Montessori
  age pages and Made in USA Toys until refilled.
- 2026-11-10: 27 TAG product URLs (linked 172 times from 30 blog/category pages, plus backlinks)
  now 301 to in-stock replacements or the closest category (docs/imports/tag_link_replacements.csv,
  applied by docs/imports/tag_redirects.php; verified 27/27 live). Old products renamed
  "...-discontinued" and kept out of stock.
- 2026-11-09: Consent Mode defaults granted outside EU/UK/CH (consent.phtml). Expect more
  Google Ads conversions and fewer GA4 "(not set)" orders from this date.
- 2026-11-09: RECOMMENDED pausing Google Ads campaigns "Name Trains Search-2026" and
  "PMAX - Carpets for Kids" (0 ROAS over Oct 1 - Nov 9); not yet confirmed done.
  Re-evaluate the two remaining PMAX campaigns around 2026-11-30.

## Title policy (2026-11-10, from scripts/title_rewrite_check.py on the top 30 pages)
Plain titles: 21 pages, Google kept 57%. Separator titles: 9 pages, kept 22%. What Google drops is the
tail after a separator (store name, "Free Shipping"); natural titles that match the H1 and are under
~60 characters are kept. Rule from now on: plain wording, a colon is fine, no "|" or "- Brand" tails.
Fixes #1, #2, #3, #4, #5, #7, #7b were retitled the same day to plain titles (8b). The SERP still showed
the old titles for #3 and #7 (not recrawled yet), so re-run the check after 2026-11-24.
