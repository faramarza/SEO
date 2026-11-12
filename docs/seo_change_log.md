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
| 8 | 2026-11-10 | /name-trains.html + 3-10 letter product pages | Category meta title "Name Trains, Personalized, Wooden and Made in the USA" (contains name trains 720, wooden name train 320; letter-count searches have 0 volume) (was truncated "Alphabet Name Train Toys Personalized Wooden Letter ..."), new meta description; product meta titles "{N} Letter Name Train, Personalized Wooden Toy Made in USA" (plain, no separators, per Frank: separator titles get rewritten) replacing "Buy ... at Discount" / "on Sale" (docs/imports/name_train_meta_update.csv, store view alphabettrains) | name train / name trains; wooden name train | #7 / #7, 2,791 impr/mo, 23 clicks/mo (0.8% CTR vs ~3% typical at #7); #4 | 2026-12-22 |

| 9 | 2026-11-10 | /alphabet-themed-carpets.html | meta title "Alphabet Rugs for Classrooms and Kids That Teach the ABCs" (rug once, no stuffing), carpet and abc rug wording moved to the meta description (was "Alphabet Carpet for Kids – Classroom & Playroom Rugs"); "abc carpet" (4,400) excluded: ABC Carpet & Home brand search | alphabet rug/rugs/carpet (1,600, one Google group); abc rug (1,600); alphabet rug for classroom (480); classroom alphabet rug (480); abc rug for classroom (320) | #31/#14/#13 (248 impr); #11; #20; #23; #26; 0 clicks on all, 90d | 2026-12-22 |

| 10 | 2026-11-10 | /montessori-toys-2-year-olds.html, /made-in-usa-montessori-toys.html | Refill after TAG closure (docs/imports/tag_refill_2yo_and_made_in_usa.csv): 2-year-olds +11 (Guidecraft Grippies/Wedgies, Whitney blocks/sensory table/mirror/light box), 35 live before; Made in USA +18 Whitney, 56 live before (110 pre-TAG). Titles "Best Montessori Toys for 2 Year Olds, Top Picks for 2026" and "Montessori Toys Made in USA, Natural Wood and Safe Finish" (no separators) | montessori toys for 2 year old(s) (~2,000 impr/mo combined); montessori toys made in usa (90 impr/mo) | #10-13, 10 clicks/mo; #2.3 at 3.7% CTR vs 15% typical | 2026-12-22 |

| 11 | 2026-11-12 | NEW /preschool-daycare-rugs.html (live, 35 rugs); /circle-time-rugs.html; /classroom-rugs/large-classroom-rugs.html; Rolling Art Cart WB0285R | New category "Preschool and Daycare Rugs" (35 Carpets for Kids rugs, docs/imports/preschool_daycare_rugs.csv); plain titles: "Circle Time Rugs for Classrooms, Preschool and Kindergarten", "Large Classroom Rugs in 6x9, 7x10 and 8x10 Sizes", "Rolling Art Cart on Wheels for Classroom Art Supplies" | preschool rugs 720, carpet for preschool 720, daycare rugs 390-480, daycare carpet 390, preschool classroom rugs 320; circle time rug 590; large classroom rugs 720; rolling art cart 590 | #12 / - / #15 / - / -; #14 (158 impr/90d); #15 (175 impr); not showing | 2026-12-23 |

7b (2026-11-10): /classroom-rugs.html meta title now "Classroom Rugs & Carpets for Teachers | Circle Time Rugs" (live).
Rug best sellers for sort order: waiting on scripts/sales_by_line.py --match rug.
Disavow: not uploaded by decision (Google de-emphasizes disavow; only needed with a manual action).

5b (2026-11-10): /classroom-furniture.html "Classroom Favorites: Whitney Brothers 2026 Best Sellers"
carousel live (8 products, server-rendered, verified).

2026-11-12: Guidecraft answered: the 11 are discontinued, sold until gone at the quoted wholesale. Restock with Guidecraft's
remaining quantities: docs/imports/guidecraft_discontinued_restock.csv.
PROPOSED (Frank's decision): 56 of 79 online Guidecraft SKUs are priced above Guidecraft MSRP (median +16%, up to +84%).
APPROVED with a margin floor: docs/imports/guidecraft_reprice_to_msrp.csv lowers 43 SKUs toward MSRP but never below a 25% NET
margin after card fee (3%) and ESTIMATED free-shipping cost ($9.50 + $0.85/lb, UPS Ground residential estimate; replace with real
Guidecraft freight when known). 6 reach MSRP, 37 land between MSRP and old price, 8 unchanged; net median 33% -> 26%, min 20% (unchanged
items). Heavy/thin items kept (G2100R, G7533, G2121B, G2151, G6111). Measure Guidecraft units/revenue 4 weeks before vs after.

Parked (on-page already fine, problem is assortment/competition): /personalized-baby-blankets.html,
personalized baby blankets 22,200/mo at #54 (11 I See Me designs vs Etsy/Amazon/Personalization Mall).

Parked (supply, not SEO): name puzzle stools. Sales ~8 orders/mo Nov 2025-Jun 2026, ~3/mo Jul-Sep, 0 in Oct and
Nov 1-10 2026, after the "12 weeks or longer" Cubby Hole lead-time warning went up (~July 2026). Heirloom stool
CH-001-1NSS = 43 of 71 orders. No page work until lead time is fixed (Hollow Woodworks lead time, firm Cubby Hole
date, or a second maker).

## Site-wide changes (affect all measurements)
- 2026-11-10: "Order by December 15 for Christmas delivery" banner (widget Name Train Christmas) on /name-trains.html and all 10 name-train product pages. Change text Dec 16, disable Dec 26. Expect a conversion lift on name trains from this date.
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
~60 characters are kept. Rule from now on: plain wording, no separators (|, dash, colon), target search first, under 60 characters.
Fixes #1, #2, #3, #4, #5, #7, #7b get plain titles (8b, given to Frank 2026-11-10): same words in the same
order as the researched titles, no separators at all (no |, dash or colon; a colon is a separator too),
written as a plain phrase with commas/"and", so the target search still leads. The SERP still showed
the old titles for #3 and #7 (not recrawled yet), so re-run the check after 2026-11-24.

Dropped (2026-11-12, serp_diagnosis): "magnetic blocks" 18,100 and "magnetic building blocks" 5,400. Page 1 is a marketplace
wall (Amazon x2, Target, Walmart, Kohl's, eBay, Toys R Us, Magna-Tiles). No page or SEO work. PowerClix is not a Magna-Tiles
substitute on price; revisit only as a small Shopping test after the Guidecraft reprice shows whether Guidecraft converts.
2026-11-12 verified live: 11 discontinued Guidecraft SKUs in stock at Guidecraft's remaining quantities; all 43 Guidecraft reprices
live. Test window: Guidecraft orders/revenue Nov 12 - Dec 10 vs Oct 15 - Nov 11 (scripts/sales_by_line.py --match guidecraft --by-month).
