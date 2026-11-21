# Project memory

## Writing conventions (strict)
- NO EM DASHES, EVER. Do not use the em dash character (Unicode U+2014) in anything: chat replies,
  drafted emails, commit messages, code comments, docs, or any other output.
  Use a period, comma, colon, parentheses, or reword the sentence instead.
  This applies to all writing produced for Faramarza (Frank).

## Servers (facts from Frank, do not second-guess)
- staging.alphabet-trains.com IS the live production Magento server. "staging" is only the hostname
  (like naming a machine "Minerva"). It is NOT a test copy. Commands run there affect the live store.
- SSH since 2026-11-13: `ssh -p 2022 atclient@staging.alphabet-trains.com`, key only. Login lands as the
  site user automatically (`who am i` = atclient, `whoami` = sttrains). Then `cd html/docroot`.
- The developer asks to be messaged before files on the live site are changed. Code changes (modules,
  templates) go through the developer; admin settings, imports and Klaviyo are Frank's.
- Governor (173.255.241.200, /var/www/seo) is a separate server and talks to Magento only via the REST API.

## Name trains (facts from Frank, do not second-guess)
- The listed price (e.g. 3 letters $29.99) is for the LETTERS ONLY. Engine and caboose are options the
  customer picks and pays extra for. Never add them to the base cost.
- Maple Landmark cost: every car $3.60 (special cars like Noah's Ark/Circus/Classic $3.70), $5 drop-ship
  handling fee per order, shipping about $8 per order ($7-9, not on their invoices). Not made to order.
- Plain train profit after the 3% card fee is about $5 to $9 (3 to 6 letters). Prices are fine.
- Before any pricing alarm or recommendation: check what the price includes, then check real orders
  (scripts/order_profit.py) and invoices. No fire drills from assumptions.
