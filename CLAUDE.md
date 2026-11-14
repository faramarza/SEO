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
