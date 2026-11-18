# Artist sources

Added on 2026-11-17 through the app's own paste code (`app.sources.parse_paste` → `resolve` → `app.db.add_sources`), so each one was normalized exactly like a paste. That's 89 sources: 36 website pages, 29 X accounts and 24 Instagram accounts.

Every X and Instagram account here is linked from the artist's official website or label page. Two exceptions: Azari has no official site, so the account comes from his TuneCore artist page, and Kocchi no Kento's X comes from label press releases that call it "OFFICIAL X".

| Artist | Website (page the scanner reads) | X | Instagram |
| --- | --- | --- | --- |
| Phantom Siita | phantomsiita.com/schedule/list, /news/1 | @PhantomSiita | @phantom_siita_official |
| Hiroyuki Sawano | sh-nzk.net/live/, /info/ | @sawano_nZk | @hiroyukisawano.official |
| Itoki Hana | 110ki.com/live | @1t0lc1 | @it0ki |
| 9Lana | sonymusic.co.jp/artist/9Lana/live/ | @9Lana2 | @9lana_staff |
| LiSA | lxixsxa.com/live | @LiSA_OLiVE | @xlisa_olivex |
| Atarashii Gakko! | atarashiigakko.com/schedule/index.html | @japanleaders | @japan_leaders |
| YOASOBI | yoasobi-music.jp/live | @yoasobi_staff | @yoasobi_staff_ |
| Ado | universal-music.co.jp/ado/news | @ado1024imokenp | — |
| Raon | universal-music.co.jp/raon/news | @raon_leee | @raon_lee |
| Kenshi Yonezu | reissuerecords.net/live | @hachi_08 | @hachi_08 |
| sukekiyo | sukekiyo-official.jp/live, /news | @sukekiyo_band | @sukekiyo_official |
| Rosu | universal-music.co.jp/roce/news | @ROSU7373 | — |
| Azari | — | @xxxAzari | — |
| Queen Bee | ziyoou-vachi.com/live/index.html | @qb_announce | @qb_announce |
| NIGHTMARE | nightmare-web.com/schedule/list, /news/1 | @nightmare_staff | @nightmare_official_jp |
| KANA-BOON | sp.kanaboon.jp/schedule/live, /news/1 | @_kanaboon | @kanaboon_official_insta |
| Eve | eveofficial.com/live | @oo0eve0oo | @eve_harapeco |
| Kocchi no Kento | sonymusic.co.jp/artist/KocchinoKento/live/ | @SuppokoPeppoko | — |
| UVERworld | uverworld.jp/live?range=future_event_end_time&sort=asc | @uverworld_dr2 | @uverworld_official |
| Haruka Tomatsu | tomatsuharuka.com/live, /info | — | — |
| ZUTOMAYO | zutomayo.net/live | @zutomayo, @zutomayo_staff | @zutomayo, @zutomayo_staff |
| MYTH & ROID | mythandroid.com/contents/schedule | @myth_and_roid | @myth_and_roid |
| Co shu Nie | coshunie.jp/pages/shows | @co_shu_nie | @coshunie_official |
| Creepy Nuts | creepynuts.com/schedule | @Creepy_Nuts_ | @creepynuts_official |
| AKASAKI | akasaki.red/live_information/schedule/list | @akasaki_0727 | @akasaki_0727 |
| Fujii Kaze | fujiikaze.com/tour, /news-article | @fujiikazestaff | @fujiikaze |
| Ai Higuchi | higuchiai.com/live | @HiguchiAi | @higuchiai.1128 |
| TRUE | true-singer.com/contents/live | @miho_karasawa | — |
| Reol | reol.jp LIVE list (/news/list/1/4) and EVENT list (/news/list/1/5) | @RRReol | @rrreol999 |

## Left out, and why

- **Haruka Tomatsu, X and Instagram:** neither her official site nor her Sony page links any account.
- **Ado, Instagram and @ado_staff:** neither is linked from the Universal or Cloud Nine pages. Only @ado1024imokenp is.
- **Kocchi no Kento, Instagram and @kocchino_info:** these aren't linked from any official page, and the Sony page has no social links.
- **Rosu, Azari, TRUE: Instagram:** none is linked from their official pages.
- **Azari, website:** there's no official site. The TuneCore page lists releases only, so there's nothing for the scanner to read.
- **Member and personal accounts:** these are official but not the artist's own or staff account, so I skipped them. Add any you want:
  - Queen Bee: アヴちゃん's @qb_avu (X and Instagram)
  - MYTH & ROID: members' Instagram @hotar_, @tom_oshima, @kihow_
  - YOASOBI: Ayase and ikura (not linked from the site)
  - ZUTOMAYO: @zutomayo_mart (merch only)

## Changes

- **2026-11-18, trailing slashes:** the first real scan got HTTP 404 on `sh-nzk.net/live`, `/info` and the Sony pages of 9Lana and Kocchi no Kento. The pages are right (they're the sites' own LIVE / NEWS links), but the paste code dropped the trailing slash and Sony's servers answer 404 without it (`/live` 404, `/live/` 200, checked in a browser). The paste code now keeps the slash, and migration 5 in `app/db.py` added it to these four sources.

## Notes for scanning

- **Paged schedules:** some schedule pages show only the current month (phantomsiita, nightmare-web, sp.kanaboon), so their NEWS page was added too.
- **UVERworld:** the 2027 dates are on page 2 of its live list.
- **Pages that need the headless browser** (they're built by JavaScript): the Sony pages (9Lana, Kocchi no Kento), lxixsxa.com, yoasobi-music.jp, ziyoou-vachi.com, creepynuts.com and higuchiai.com.
- **Identity:** Rosu (ロス/Roce) and Azari are still the identification from `docs/SWEEP.md`, not yet confirmed by Daniel.
