# BOSS recruiter favorite-list contract fixtures

These files are synthetic contract fixtures, not raw BOSS captures.

They contain only behavior already confirmed by historical evidence and the
business owner:

- recruiter favorite-list responses use `zpData.cardList` and `hasMore`;
- candidates can be identified by stable `encryptGeekId` values;
- pages are ordered from newest favorite to oldest favorite;
- the web list displays 10 candidates per page and at most 400 candidates;
- no favorite timestamp field has been confirmed.

The identifiers are fictional. Names, avatars, resumes, `securityId`, cookies,
tokens, and other account data are deliberately absent. These fixtures must not
be cited as evidence for any field or behavior not listed above.

`page_1_has_more.json` and `page_2_end.json` model an old ten-ID anchor group
that now spans two pages after two newer favorites were prepended. The ordered
anchor is `anchor-01` through `anchor-10`.

`page_40_has_more.json` models a contract anomaly: the final allowed page still
claims that another page exists. A safe sync must stop incomplete and must not
request page 41.
