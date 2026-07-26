# Nezávislý audit `value-betting-poc`

**Datum auditu:** 25. července 2026  
**Jazyk:** čeština  
**Auditovaný commit:** `e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1`  
**Předmět:** zdrojový kód, metodika, historie vývoje, release databáze,
živý dashboard, měření ROI a návrh dalšího řešení

## 1. Výrok auditu

### Krátká odpověď

Program je funkční proof of concept: umí získat kurzy ze tří webů, párovat
zápasy, najít cenové rozdíly, uložit jejich průběh, vyhodnotit výsledek sázky
a vytvořit dashboard. Výpočty edge, settlementu a P&L pro uložené řádky jsou
většinou aritmeticky správné. Testy aktuální verze procházejí.

Současné výsledky však **neprokazují ziskovou value-betting strategii** a
nejsou vhodné pro rozhodnutí o reálném kapitálu. Důvody:

1. Pipeline dovolí porovnat staré a časově vzdálené kurzy. Neexistuje limit
   stáří ani časového rozdílu mezi bookmakery.
2. Identita a ukládání opportunity instance při restartu procesu mohou
   přepsat starou historii novým crossingem. Tuto chybu jsem reprodukoval.
3. Výchozí pětiminutový `convergence` filtr používá budoucí průběh
   příležitosti. Není to simulace vstupu, který šel v danou chvíli provést.
4. Dashboard Method B nevyhodnocuje samostatný Method-B signál. Vybere jen
   případy, které měly Method B nad prahem už při vstupu Method A, a opomene
   pozdější Method-B crossingy.
5. Za posledních 24 hodin snapshotu proběhlo 34 GitHub Actions běhů, ačkoli
   cron definice plánovaly 361 firingů. Medián mezery byl 35,57 minuty,
   maximum 194,03 minuty.
6. Jeden denní handicap capture skutečně získal data, ale release upload
   selhal kvůli oprávnění. Další běh obnovil starou cache a celý capture se
   ztratil.
7. Z 59 settled legs v nefiltrované Method-A kohortě jich 23 vstoupilo před
   prvním commitem a všech 59 před hlavními opravami duplicit. Žádný
   vyhodnocený vstup nelze připsat auditovanému HEAD kódu.
8. Vzorek je malý a korelovaný. Výchozí 42 legs představuje jen 25 unikátních
   zápasů. Event-cluster 95% interval ROI je přibližně −40,0 % až +42,8 %.
9. Method A používá vigged Pinnacle kurz jako fair kurz. To není odhad
   očekávané hodnoty. Method B odstraní celkový overround, ale proportional
   de-vig neodstraní favorite–longshot bias.
10. Databáze neobsahuje auditovatelný záznam provedené sázky, přijatý kurz,
    skutečný limit, slippage, odmítnutí, verzi kódu ani úplný původ
    settlementu.

### Rozhodnutí

| Otázka | Výsledek |
|---|---|
| Spustí se aktuální kód a procházejí testy? | Ano, po doplnění `tzdata` v auditním prostředí |
| Odpovídá dashboard vlastnímu výpočtu? | Ano, u hlavních Method-A čísel |
| Je Method B na dashboardu správná samostatná strategie? | Ne |
| Je výchozí `convergence = 5 min` nasaditelná simulace? | Ne |
| Jsou settlement a P&L uložených řádků aritmeticky správné? | Ano |
| Lze současné ROI připsat auditovanému kódu? | Ne |
| Prokazují data kladné očekávané ROI? | Ne |
| Je systém připraven pro reálné sázení? | Ne |

**Doporučení auditu: `NO-GO` pro reálné peníze a pro veřejné tvrzení o
prokázaném kladném ROI.** Nejprve je nutné opravit sběr, časovou
synchronizaci, identitu rozhodnutí, online entry logiku a datový původ.
Poté musí začít nový, předem specifikovaný paper-trading test. Starý vzorek
lze ponechat jen jako vývojový.

To neznamená, že hypotéza value bettingu je chybná. Znamená to, že tento
experiment ji zatím spolehlivě netestuje.

## 2. Rozsah a zmrazené vstupy

Audit vychází z těchto lokálně uložených artefaktů:

| Artefakt | Identifikace |
|---|---|
| Repozitář | `sources/value-betting-poc` |
| Větev | `master` |
| Commit | `e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1` |
| Poslední commit | 2026-07-25 20:52:43 CEST |
| Historie | 28 commitů |
| Release DB | `sources/release-data/vb.sqlite` |
| Velikost DB | 157 364 224 B |
| SHA-256 DB | `7FE5770104CBFCF974174210BACC1CD8D8BFC4CEB87AFBEB08C8C995E07AD504` |
| Zachycený dashboard | `sources/live-dashboard/index.html` |
| Velikost dashboardu | 452 129 B |
| SHA-256 dashboardu | `45567FB182F411B575A0B5812289365E2112ED703DAA52210764675CFFDD714A` |

Repozitář byl stažen s celou dostupnou historií. Release databáze a dashboard
byly staženy z odkazů v zadání. Audit neměnil upstream repozitář ani jeho
data.

Prostudované podklady:

- `PROJECT_DOCUMENTATION.md`;
- `VB - methodology.docx`;
- `VB - methodology - addendum.md`;
- `VPS_MIGRATION_PLAN.md`;
- všech 28 commitů včetně podrobných commit messages;
- produkční moduly `vb/`, pomocné skripty, workflow a testy;
- živý dashboard a jeho JavaScript;
- release SQLite databáze;
- veřejné záznamy GitHub Actions a vzorek oficiálních výsledků zápasů.

### Co audit nemůže zpětně dokázat

Raw snapshoty starší než 24 hodin se průběžně mažou. Databáze také neukládá
identifikátor capture runu, commit SHA u rozhodnutí, odpověď scraperu ani
zdrojovou adresu u většiny settlementů. Proto nelze:

- přesně rekonstruovat všechny historické kurzy a všechny signály;
- určit, která verze programu vytvořila každý opportunity řádek;
- ověřit přesný pár benchmark/comparison snapshotů, který původní běh použil;
- změřit historické slippage, odmítnutí a skutečně přijaté limity;
- nezávisle doložit každý manuální settlement jen z DB;
- opravit vadné timestampy bez domněnek.

Kde není možný důkaz, report uvádí omezení místo odhadu.

## 3. Jak systém skutečně funguje

### 3.1 Datový tok

```text
GitHub Actions cron / ruční spuštění
            │
            ▼
  Pinnacle → Swisslos → Loro scrapers
            │
            ▼
 raw_event + raw_market_snapshot
            │
            ▼
  fuzzy event matching proti Pinnacle
            │
            ▼
 poslední známý benchmark + poslední comparison market
            │
            ▼
 Method A a proportional de-vig Method B
            │
            ▼
 OpportunityTracker (3% Method-A crossing)
            │
            ▼
 opportunity + opportunity_snapshot
            │
            ├── ESPN / manuální výsledky → settlement
            │
            └── evaluation.py → statický HTML dashboard
```

1. Pinnacle je benchmark.
2. Swisslos a Loro jsou comparison books.
3. Každý scraper ukládá normalizované eventy a market snapshoty.
4. Matching hledá protějšek stejného zápasu mezi weby.
5. Pipeline načte **poslední známý** market z každého webu.
6. Pro každou selection spočítá Method A a Method B.
7. Opportunity se otevře pouze podle Method A, jakmile `edge_a >= 3 %`.
8. Další snapshoty se přidávají, dokud edge neklesne pod práh, nezačne zápas
   nebo se market neoznačí jako suspended.
9. Settlement se uloží podle eventu, marketu, line a selection.
10. Dashboard dodatečně filtruje entry threshold a délku konvergence.

### 3.2 Výpočty edge

Označme:

- \(O_b\): publikovaný benchmark kurz;
- \(O_c\): comparison kurz;
- \(q_i = 1/O_{b,i}\): hrubou implikovanou pravděpodobnost benchmarku;
- \(R = \sum_i q_i - 1\): benchmark overround.

Method A:

\[
edge_A = \frac{O_c}{O_b} - 1
\]

Method B s proportional de-vig:

\[
p_i = \frac{q_i}{\sum_j q_j}
\]

\[
edge_B = O_c p_i - 1
\]

Pro tutéž selection z toho plyne:

\[
edge_B = \frac{1 + edge_A}{1 + R} - 1
\]

Tento vztah je důležitý. Method B zde není nezávislý zdroj informace; je
deterministická transformace Method A a benchmark overroundu.

Při mediánu benchmark overroundu 6,969 % znamená Method-A práh 3 %:

\[
edge_B \approx \frac{1.03}{1.06969}-1 = -3.71\%
\]

Pro Method-B edge 3 % je při stejném overroundu nutný Method-A edge přibližně
10,18 %. Proto většina Method-A vstupů není kladná podle vlastního fair-price
modelu systému.

### 3.3 Opportunity lifecycle

Tracker drží stav v paměti procesu:

- `IDLE` — signál je pod prahem;
- `OPEN` — edge je nad prahem;
- close — pokles pod práh, začátek zápasu nebo suspension;
- nový crossing — nová instance.

Instance ID má tvar odvozený z `market_key` a lokálního čítače `#1`, `#2`, …
Čítač se při každém novém procesu vrátí na nulu. Z databáze se obnoví jen
otevřená instance. To je kořen reprodukované chyby přepisu historie.

### 3.4 Settlement a P&L

Settlement podporuje:

- match winner;
- totals;
- Asian handicap;
- win, loss, push, half-win a half-loss.

Flat profit jedné jednotky:

| Výsledek | Profit |
|---|---:|
| won | \(O-1\) |
| lost | \(-1\) |
| push | \(0\) |
| half-won | \((O-1)/2\) |
| half-lost | \(-0.5\) |

U všech 54 settlement řádků uložený outcome odpovídá znovu provedenému
settlement výpočtu. P&L všech 59 vyhodnocených opportunity legs odpovídá
výše uvedeným pravidlům.

### 3.5 Rozdíl proti původní metodice

| Původní požadavek | Skutečný stav |
|---|---|
| Pinnacle, Betfair, Swisslos, Loro | Pinnacle, Swisslos, Loro; Betfair chybí |
| Obnova alespoň jednou za minutu | nominálně 5/20 min a denní AH; skutečně často 30+ min |
| Plný snapshot všech čtyř knih | persistentní opportunity JSON obsahuje jen benchmark a jednu comparison site |
| Bezpodmínečný úplný capture | raw historie se po 24 hodinách maže |
| Vyhodnocení hodnoty proti fair pravděpodobnosti | Method A používá vigged kurz; Method B jen proportional de-vig |

Implementace tedy netestuje původní metodiku v plném rozsahu.

## 4. Postup ověření

Audit použil několik nezávislých vrstev:

1. četba dokumentace, kódu, testů a historie commitů;
2. spuštění celého test suite v izolovaném prostředí;
3. syntax/bytecode kontrola přes `compileall`;
4. read-only SQL kontroly databáze;
5. vlastní přepočet edge, overroundu, settlementu, P&L a dashboardových
   filtrů;
6. cílené minimální reprodukce restartu trackeru, stale dat a event-start
   close;
7. srovnání Python evaluátoru a JavaScript dashboardu;
8. event-cluster bootstrap a citlivostní analýza;
9. kontrola skutečné cadence přes DB a GitHub Actions;
10. ruční kontrola vzorku skóre proti oficiálním klubovým a soutěžním webům.

### 4.1 Testy a statická kontrola

Výsledek:

- `146 passed in 5.76s`;
- produkční line coverage: 84 %;
- `compileall`: bez chyby;
- živý dashboard: bez chyby v konzoli;
- Ruff s výchozími pravidly: 85 nálezů, většinou styl a modernizace.

Nejnižší coverage mají scrapers a získávání výsledků: přibližně 55–70 %.
Core výpočty mají 86–100 %. Testy nepokrývaly rozhodující restartové,
časové a merge scénáře popsané níže.

Na Windows testy nejprve selhaly při `ZoneInfo("Europe/Zurich")`, protože
`requirements.txt` neobsahuje `tzdata`. Auditní prostředí ji doplnilo. Na
Ubuntu může fungovat systémová timezone databáze, ale závislost není
přenosná. Všechny položky v `requirements.txt` jsou navíc bez verze a soubor
míchá runtime a testovací závislosti.

### 4.2 Integrita databáze

- `PRAGMA integrity_check`: `ok`;
- `PRAGMA foreign_key_check`: 0 porušení;
- všechny raw outcome JSON dokumenty jsou syntakticky platné;
- všechny očekávané odds jsou větší než 1;
- všech 531 `full_market_json` dokumentů je platných;
- uložené edge A, edge B a overround se shodují s nezávislým přepočtem;
- všech 54 settlement outcomes se shoduje s novým settlement výpočtem.

SQLite soubor tedy není fyzicky poškozen. Hlavní problémy jsou s významem,
původem a časovou konzistencí dat.

## 5. Forenzní stav dat

### 5.1 Počty a časový rozsah

| Tabulka | Řádků |
|---|---:|
| `raw_event` | 4 438 |
| `raw_market_snapshot` | 656 354 |
| `opportunity` | 175 |
| `opportunity_snapshot` | 531 |
| `settlement` | 54 |
| `event_match_review` | 88 |

Rozsah raw capture:

- začátek: `2026-07-23T15:00:21Z`;
- konec: `2026-07-25T18:47:31Z`;
- délka: méně než 52 hodin.

Opportunity stavy:

| Stav | Počet |
|---|---:|
| open | 70 |
| dropped below threshold | 61 |
| event started | 44 |
| market suspended | 0 |

`market_suspended` je v produkčním toku prakticky nedosažitelný. Pipeline
nenastavuje příslušný flag a chybějící nový market se maskuje použitím
posledního známého snapshotu.

### 5.2 Raw coverage

| Web | Market | Raw snapshotů | Eventů |
|---|---|---:|---:|
| Loro | match winner | 3 802 | 240 |
| Pinnacle | Asian handicap | 308 523 | 3 682 |
| Pinnacle | match winner | 34 221 | 3 079 |
| Pinnacle | totals | 296 981 | 3 672 |
| Swisslos | Asian handicap | 39 | 11 |
| Swisslos | match winner | 6 959 | 516 |
| Swisslos | totals | 5 829 | 443 |

39 Swisslos Asian-handicap řádků proti 308 523 Pinnacle řádkům není reálné
pokrytí tohoto marketu. Je to důsledek denního capture a ztraceného
handicap runu.

Max-bet údaj chybí u 239 z 531 opportunity snapshotů:

- Loro: 39;
- Swisslos: 200.

Později přidané hodnoty 500/1000 jsou webové stropy, ne důkaz, že bookmaker
takovou konkrétní sázku skutečně přijal.

### 5.3 Matching review

| Stav review | Počet |
|---|---:|
| approved | 64 |
| pending | 24 |
| rejected | 0 |

Z approved je 58 Loro a 6 Swisslos. Approved skóre leží mezi 0,7119 a 0,8984,
medián je 0,8412. Pending medián je 0,7351.

Dataset obsahuje jen kladně schválené páry a žádné zamítnuté páry. Nelze z něj
změřit false-positive rate, recall ani statisticky obhájit práh 0,70. Tři
pending páry jsou podezřelé prohozením home/away; žádný approved pár nebyl
takto označen.

### 5.4 Opportunity pokrytí

| Rozměr | Počet |
|---|---:|
| celkem | 175 |
| Swisslos | 125 |
| Loro | 50 |
| match winner | 163 |
| totals | 8 |
| Asian handicap | 4 |

Výsledek je téměř celý match-winner experiment. Závěry o totals nebo
handicap strategii by vycházely z jednotek případů.

### 5.5 Settlement pokrytí

| Market | Settled legs |
|---|---:|
| match winner | 45 |
| totals | 5 |
| Asian handicap | 4 |

| Outcome | Počet |
|---|---:|
| won | 19 |
| lost | 33 |
| push | 2 |

Jde o 54 settlement keys, ale evaluace přes ně pokrývá 59 opportunity legs,
protože více opportunity řádků může odkazovat na stejný výsledek. Celkem jde
o 32 unikátních settled zápasů.

Settlement sources:

- 28× `manual:websearch`;
- 18× `auto:espn`;
- 8× `manual`.

Vzorek výsledků jsem porovnal s oficiálními zdroji UEFA, klubů a Swiss
Football League; kontrolované skóre souhlasilo. Databáze však neukládá URL,
provider event ID, čas získání, raw response hash ani identitu schvalovatele.
Správnost celých 54 řádků tak není z DB samostatně dokazatelná.

## 6. Jsou současná čísla správná?

### 6.1 Method A

Ano z hlediska aritmetiky současného programu. Ne z hlediska čistého,
nasaditelného odhadu výnosu.

#### Bez convergence filtru

Nastavení:

- entry threshold 3 %;
- min. converge time 0 min;
- všechny weby, markety a odds buckety;
- flat stake 1 jednotka.

| Metrika | Hodnota |
|---|---:|
| identifikované opportunities | 175 |
| open | 70 |
| resolved | 105 |
| settled evaluované legs | 59 |
| čeká na výsledek | 116 |
| unikátních settled zápasů | 32 |
| wins / losses / pushes | 19 / 38 / 2 |
| zisk | +5,65 u |
| flat ROI | +9,576 % |
| zobrazený hit rate | 33,333 % |

Rozpad:

| Skupina | N | Profit | ROI |
|---|---:|---:|---:|
| favorite | 11 | +0,45 u | +4,09 % |
| mid | 27 | +3,30 u | +12,22 % |
| longshot | 21 | +1,90 u | +9,05 % |
| Loro | 20 | −4,60 u | −23,00 % |
| Swisslos | 39 | +10,25 u | +26,28 % |
| match winner | 50 | +7,10 u | +14,20 % |
| totals | 5 | −0,85 u | −17,00 % |
| Asian handicap | 4 | −0,60 u | −15,00 % |

Součet profitů i stake odpovídá dashboardu a nezávislému výpočtu.

#### Výchozí dashboard: convergence 5 min

| Metrika | Hodnota |
|---|---:|
| identifikované opportunities | 83 |
| settled evaluované legs | 42 |
| čeká na výsledek | 41 |
| unikátních settled zápasů | 25 |
| wins / losses | 13 / 29 |
| zisk | −0,25 u |
| flat ROI | −0,595 % |
| hit rate | 30,952 % |

Rozpad:

| Skupina | N | Profit | ROI |
|---|---:|---:|---:|
| favorite | 5 | +1,90 u | +38,00 % |
| mid | 20 | +4,65 u | +23,25 % |
| longshot | 17 | −6,80 u | −40,00 % |
| Loro | 20 | −4,60 u | −23,00 % |
| Swisslos | 22 | +4,35 u | +19,77 % |
| match winner | 41 | −1,25 u | −3,05 % |
| totals | 1 | +1,00 u | +100,00 % |
| Asian handicap | 0 | — | — |

Výchozí headline `−0,6 %` je tedy správně zaokrouhlené číslo pro přesně tu
množinu řádků, kterou dashboard vybral.

### 6.2 Proč convergence filtr neměří proveditelnou strategii

Dashboard nejprve vezme kurz z prvního snapshotu, kde opportunity vznikla.
Poté se zpětně podívá, jak dlouho opportunity zůstala otevřená, a zahodí ji,
pokud konečná délka byla menší než pět minut.

V okamžiku prvního crossingu ale nebylo známo, zda edge vydrží pět minut.
Strategie nemůže:

1. vsadit původní kurz v čase \(t_0\);
2. v čase \(t_0+5\) minut zjistit, zda signál vydržel;
3. zpětně zrušit sázku, pokud nevydržel.

Správné online pravidlo by čekalo pět minut a sázelo až v \(t_0+5\) za kurz,
který je dostupný tehdy. Současný dashboard používá budoucí informaci, ale
ponechá starý vstupní kurz.

V release DB convergence filtr odstraní přesně 17 settled legs. Všech 17 má
naměřenou délku přesně nula minut; žádná nemá 1–4 minuty. Těchto 17 legs
vydělalo +5,90 u, ROI +34,706 %. To vysvětluje změnu z +5,65 u na −0,25 u.
Není to důkaz, že krátké signály jsou lepší. Část nulových délek vznikla
vadným časovým lifecyclem.

Filtr lze ponechat jako **popisnou kohortu**, musí se však označit
`retrospective duration`, nikoli P&L simulace. Pro strategii se musí zavést
nový čas rozhodnutí a kurz při skutečném vstupu.

### 6.3 Method B: aritmeticky konzistentní, funkčně chybná

Současný kód:

1. najde první snapshot Method-A opportunity;
2. vezme `edge_b` právě z tohoto snapshotu;
3. zahrne řádek do Method B jen tehdy, pokud už tehdy `edge_b >= threshold`.

Neprojde celou trajektorii a nenajde první okamžik, kdy samotná Method B
překročila práh. Proto Method B na dashboardu není samostatná vstupní metoda.

#### Výsledek současného kódu

| Nastavení | N | Profit | ROI | Hit rate |
|---|---:|---:|---:|---:|
| convergence 0 min | 7 | +2,50 u | +35,714 % | 42,86 % |
| convergence 5 min | 5 | +2,35 u | +47,000 % | 40,00 % |

Tato čísla lze reprodukovat, ale popisek „Method B agrees“ zakrývá omezení:
jde o podmnožinu Method-A entry okamžiků.

#### Skutečný replay prvního Method-B crossingu

Z 175 opportunities:

- 16 mělo `edge_b >= 3 %` už v Method-A vstupním snapshotu;
- 24 dosáhlo `edge_b >= 3 %` někdy během uložené trajektorie;
- současný evaluátor tedy opomene 8 případů.

Ze settled dat:

- současná Method-B podmnožina má 7 legs;
- skutečný první Method-B crossing má 12 legs;
- opomenuto je 5 settled legs.

| Method-B definice | Convergence | N | Profit | ROI | Zápasů |
|---|---:|---:|---:|---:|---:|
| současný kód | 0 min | 7 | +2,50 u | +35,714 % | 7 |
| první skutečný B crossing | 0 min | 12 | +1,10 u | +9,167 % | 10 |
| současný kód | 5 min | 5 | +2,35 u | +47,000 % | 5 |
| první skutečný B crossing | 5 min | 10 | +0,95 u | +9,500 % | 8 |

Pět opomenutých settled vstupů mělo dohromady profit −1,40 u. Jedna dvojice
patří mezi překrývající se opportunity instance, takže ani opravených
+9,17 % nelze brát jako čistý výsledek. Důkazem chyby je rozdíl ve vstupní
definici, ne konkrétní směr změny ROI.

Raw historie starší než 24 hodin chybí. Nelze proto zpětně najít případy, kdy
Method B překročila práh mimo trajektorii otevřenou Method A. Pro aktuální
proportional model s kladným benchmark overroundem sice platí
`edge_b <= edge_a`, takže B-only crossing při stejném prahu nevznikne, ale
po změně de-vig/modelu tento předpoklad platit nemusí. Capture musí být
oddělený od Method-A lifecycle.

### 6.4 Citlivost na threshold

Při pětiminutovém retrospektivním filtru:

| Threshold | Settled N | Profit | ROI |
|---:|---:|---:|---:|
| 3,0 % | 42 | −0,25 u | −0,595 % |
| 4,0 % | 31 | +1,05 u | +3,387 % |
| 5,0 % | 25 | +2,75 u | +11,000 % |
| 7,5 % | 16 | +5,15 u | +32,188 % |
| 10,0 % | 11 | +2,05 u | +18,636 % |
| 15,0 % | 5 | −5,00 u | −100,000 % |

Tabulka ukazuje nestabilitu malého vzorku. Nelze z ní vybrat 7,5 % jako
„lepší“ threshold. Dashboard umožňuje mnoho kombinací thresholdu, webu,
marketu, odds bucketu a stakingu. Nejlepší zobrazená kombinace je post-hoc
maximum a má selection bias. Parametr je nutné zmrazit před novými daty.

### 6.5 Kelly

Současný Kelly vzorec pro binární win/loss je algebraicky správný, pokud je
pravděpodobnost \(p\) správná:

\[
f^* = \frac{pO - 1}{O - 1}
\]

Dashboard ale nedělá plnou bankroll simulaci:

- stake je podíl z jedné referenční flat jednotky;
- bankroll se mezi sázkami neaktualizuje;
- není modelována souběžná expozice;
- není drawdown ani risk of ruin;
- nejsou zahrnuty limity, slippage a odmítnutí;
- jednoduchý binární vzorec neřeší obecnou distribuci push/half outcomes;
- \(p\) pochází z nekalibrovaného de-vig Pinnacle kurzu.

Výsledek:

| Metoda | N | Celkem staked | Profit | ROI na staked |
|---|---:|---:|---:|---:|
| A, 0 min | 59 | 0,4593 u | +0,0350 u | +7,61 % |
| B dle kódu, 0 min | 7 | 0,0364 u | +0,0281 u | +77,16 % |

Method-B Kelly ROI 77 % je poměr malého profitu k velmi malému součtu stake.
Není to 77% růst bankrollu. V UI by se metrika měla jmenovat
`profit / total simulated stake`, ne „bankroll ROI“.

Pro quarter-line, push a jiné vícevýsledkové návraty se má stake hledat jako:

\[
\arg\max_f \sum_s p_s \log(1 + f r_s)
\]

kde \(r_s\) je čistý návrat v každém možném stavu.

### 6.6 Hit rate

Dashboard při half-win připisuje 0,75 hitu a při half-loss 0,25 hitu. To je
zvolená prezentační konvence, nikoli standardní pravděpodobnost výhry. ROI
settlement half-stake je správný, ale hit rate by měl uvádět oddělené počty
W/HW/P/HL/L nebo jasně pojmenovaný `result score`.

## 7. Statistická průkaznost

### 7.1 Základní intervaly

Audit použil:

- Wilsonův 95% interval pro binární hit rate;
- 200 000 bootstrap replik;
- pevný seed `20260725`;
- resampling po **zápasech**, nikoli po legs, aby se zachovala korelace
  více sázek téhož eventu;
- v každé replice ROI = součet profitů / součet flat stakes.

| Kohorta | Legs | Eventů | W/L/P | ROI | 95% cluster interval ROI |
|---|---:|---:|---:|---:|---:|
| Method A, 0 min | 59 | 32 | 19/38/2 | +9,576 % | −26,15 % až +50,77 % |
| Method A, 5 min | 42 | 25 | 13/29/0 | −0,595 % | −40,00 % až +42,75 % |
| Method B dle kódu, 0 min | 7 | 7 | 3/4/0 | +35,714 % | −69,29 % až +162,14 % |
| Method B dle kódu, 5 min | 5 | 5 | 2/3/0 | +47,000 % | −100,00 % až +209,00 % |
| Method B, skutečný crossing, 0 min | 12 | 10 | — | +9,167 % | −74,29 % až +126,50 % |
| Method B, skutečný crossing, 5 min | 10 | 8 | — | +9,500 % | −77,50 % až +153,13 % |

Wilson 95% interval hit rate:

| Kohorta | Hit rate | 95% interval |
|---|---:|---:|
| Method A, 0 min | 33,33 % | 22,49 % až 46,28 % |
| Method A, 5 min | 30,95 % | 19,07 % až 46,03 % |
| Method B dle kódu, 0 min | 42,86 % | 15,82 % až 74,95 % |
| Method B dle kódu, 5 min | 40,00 % | 11,76 % až 76,93 % |

Hit rate bez zohlednění kurzů sama o sobě neříká, zda je strategie zisková.
Intervaly zde jen ukazují velikost nejistoty.

Bootstrap podíl replik s ROI nad nulou:

- Method A, 0 min: 68,13 %;
- Method A, 5 min: 48,11 %.

Tyto hodnoty nejsou validní post-hoc p-value. Jen ukazují, že data připouštějí
široké rozpětí kladných i záporných výsledků.

### 7.2 Citlivost na jeden zápas

Leave-one-event-out rozsah ROI:

| Kohorta | Minimum | Maximum |
|---|---:|---:|
| Method A, 0 min | −0,603 % | +13,636 % |
| Method A, 5 min | −9,875 % | +7,051 % |
| Method B dle kódu, 0 min | −9,167 % | +58,333 % |
| Method B dle kódu, 5 min | −17,500 % | +83,750 % |

Method A bez convergence filtru nezávisí na jediném vítězném eventu tak
extrémně jako Method B, ale 32 eventů stále nestačí pro přesný odhad.

### 7.3 Citlivost na datové anomálie

V datech jsou dvě dvojice překrývajících se opportunity instancí stejného
`market_key`:

- Eintracht Braunschweig – Southampton, Loro home;
- Al-Ettifaq – York City, Loro away.

Obě instance v každé dvojici se časově překrývají a obě se započítaly jako
prohrané sázky. Nejde o dvě prokazatelně oddělená rozhodnutí.

Citlivostní výpočet po odstranění jedné instance z každé dvojice:

| Kohorta | N | Profit | ROI |
|---|---:|---:|---:|
| A, 0 min | 57 | +7,65 u | +13,421 % |
| A, 5 min | 40 | +1,75 u | +4,375 % |

Další citlivost: vyloučení všech devíti settled legs, u kterých
`opportunity.first_cross_at` nesouhlasí s prvním dochovaným snapshotem:

| Kohorta | N | Profit | ROI |
|---|---:|---:|---:|
| A, 0 min | 50 | +6,00 u | +12,000 % |
| A, 5 min | 33 | +0,10 u | +0,303 % |

Tyto tabulky nejsou „opravené ROI“. Ukazují, že datová definice mění výsledek
o několik procentních bodů a že nelze bezpečně vybrat jednu variantu bez
auditovatelného decision logu.

### 7.4 Závěr statistického auditu

Současný vzorek:

- je vývojový a částečně vznikl před verzovaným kódem;
- pokrývá méně než 52 hodin;
- má jen 25–32 settled eventů podle filtru;
- obsahuje více legs a duplicitních instancí na event;
- dovoluje post-hoc volbu mnoha filtrů;
- nezahrnuje náklady skutečného provedení.

Kladné ROI v jedné kohortě a záporné ROI v druhé jsou obě slučitelné s
náhodou. Nulové ani kladné expected ROI není tímto vzorkem potvrzeno.

## 8. Registr programových a funkčních nálezů

Priority:

- **P0:** výsledek nebo rozhodnutí mohou být věcně chybné; blokuje nový
  experiment;
- **P1:** významně zkresluje data, audit nebo provoz;
- **P2:** omezuje přesnost, bezpečnost nebo údržbu;
- **P3:** dokumentace, ergonomie nebo nižší provozní riziko.

### Souhrn

| ID | Priorita | Nález | Stav důkazu |
|---|---|---|---|
| F-01 | P0 | Bez freshness a skew limitu | potvrzeno kódem a reprodukcí |
| F-02 | P0 | Restart trackeru může přepsat starou instanci | reprodukováno |
| F-03 | P0 | Chybný čas a stale snapshot při `event_started` | reprodukováno |
| F-04 | P0 | Method B používá chybný entry okamžik | potvrzeno kódem a daty |
| F-05 | P0 | Convergence filtr má look-ahead bias | potvrzeno kódem |
| F-06 | P0 | Aktuální ROI nelze připsat aktuálnímu kódu | potvrzeno timestampy |
| F-07 | P0 | GitHub schedule zahazuje/odkládá běhy | potvrzeno workflow a runy |
| F-08 | P0 | Denní handicap data se po HTTP 403 ztratila | potvrzeno run logy |
| F-09 | P1 | Překrývající se instance se počítají jako více sázek | potvrzeno DB |
| F-10 | P1 | Pruning zachovává `MAX(id)`, ne nejnovější čas | potvrzeno DB |
| F-11 | P1 | Merge může zahodit opravy a snapshoty | potvrzeno kódem |
| F-12 | P1 | Method A není fair-EV model | matematicky potvrzeno |
| F-13 | P1 | Proportional Method B neřeší favorite–longshot bias | metodický fakt |
| F-14 | P1 | Greedy matching a home/away orientace | potvrzeno kódem |
| F-15 | P1 | Chybí decision/execution/provenance log | potvrzeno schématem |
| F-16 | P1 | Catch-all chyby mohou vytvořit zelený vadný run | potvrzeno kódem |
| F-17 | P2 | Settlement evidence není auditovatelná | potvrzeno schématem |
| F-18 | P2 | Kelly UI není bankroll simulace | potvrzeno kódem |
| F-19 | P2 | Parsers/settlement nevalidují všechny varianty | potvrzeno kódem |
| F-20 | P2 | Závislosti nejsou reprodukovatelné | potvrzeno instalací |
| F-21 | P2 | Pre-entry historie nereprodukuje původní rozhodnutí | potvrzeno kódem |

## 9. P0 nálezy

### F-01 — Pipeline porovnává libovolně staré kurzy

**Důkaz**

`load_latest_market_snapshots()` záměrně vrací poslední známý snapshot
„however long ago“. Pipeline vezme poslední benchmark i comparison řádek bez:

- maximálního stáří;
- maximálního rozdílu timestampů;
- potvrzení, že oba zdroje uspěly ve stejném capture runu;
- informace o latenci requestu;
- odmítnutí po částečném selhání scraperu.

Relevantní kód:

- `vb/storage.py:173–210`;
- `vb/pipeline.py:216–224`;
- `scripts/scheduled_run.py:174–208`.

**Cílená reprodukce**

- benchmark snapshot: starý 1 hodinu;
- comparison snapshot: starý 20 hodin;
- rozdíl mezi nimi: 19 hodin;
- běh před kickoffem;
- výsledek: pipeline otevřela opportunity s `edge_a = 9,5238 %` a jako
  `first_cross_at` použila 20 hodin starý comparison čas.

Toto je faktická chyba. Z nalezeného rozdílu nelze určit, zda byl kurz
současně dostupný. Může jít o dávno staženou cenu, ne o value.

**Přesná oprava**

Do každého raw snapshotu přidat:

- `capture_run_id`;
- `source_run_id`;
- `observed_at` z odpovědi, pokud existuje;
- `received_at` z lokálních monotónních/UTC hodin po přijetí;
- volitelně `request_started_at` a `request_finished_at`.

Pipeline musí před výpočtem fail-closed ověřit:

```text
benchmark source_run.status == success
comparison source_run.status == success
now - benchmark.received_at <= max_age
now - comparison.received_at <= max_age
abs(benchmark.observed_at - comparison.observed_at) <= max_skew
kickoff - now >= minimum_lead_time
event match je approved nebo auto nad kalibrovaným prahem
```

Po migraci na minutový VPS cyklus navrhuji počáteční limity:

- `max_age = 90 s`;
- `max_skew = 60 s`;
- `minimum_lead_time = 5 min`.

Nejde o univerzální optimální hodnoty. Musí se zmrazit v konfiguraci
`strategy_version`, měřit jejich reject rate a měnit jen v novém
walk-forward experimentu. Dokud systém běží na GitHub Actions s třiceti-
minutovými mezerami, nelze těmito limity získat dost signálů; proto oprava
vyžaduje i F-07.

Při pomalém full Swisslos sweepu se má rychlý benchmark načíst znovu po
sweepu nebo se má párovat každý comparison snapshot s časově nejbližším
benchmark snapshotem, ne s posledním řádkem obecně.

**Testy**

1. stale benchmark → žádné rozhodnutí, důvod `benchmark_stale`;
2. stale comparison → `comparison_stale`;
3. obě fresh, skew nad limitem → `snapshot_skew`;
4. jeden source run failed → žádné rozhodnutí;
5. přesně na hranici limitu → deterministicky specifikovaný výsledek;
6. nový benchmark po dlouhém sweepu se vybere před starým;
7. rejected signály se uloží jako auditní observations, ne jako bet.

### F-02 — Restart trackeru může přepsat starou opportunity

**Kořen chyby**

`OpportunityTracker` inicializuje `_instance_seq = 0`. `run_cycle()` obnoví
jen otevřenou opportunity. Po dřívější uzavřené instanci, poklesu a novém
crossingu v novém procesu tak vznikne znovu instance `#1`.

`save_opportunity()` narazí na existující primary key. Aktualizuje část
hlavičky a smaže/nahradí snapshoty podle nové in-memory instance. Stabilní
historie se změní.

Relevantní kód:

- `vb/opportunity.py:158–207`;
- `vb/opportunity.py:209–285`;
- `vb/storage.py:230–288`.

**Cílená reprodukce**

1. proces A otevřel `#1` v `00:00`;
2. zavřel ji v `00:05`;
3. nový proces B zaznamenal nový crossing v `00:10`;
4. proces B znovu vytvořil `#1`.

Po uložení:

- hlavička stále nesla původní `first_cross_at = 00:00`;
- jediný dochovaný snapshot měl čas `00:10`;
- původní trajektorie byla pryč.

V release DB tento fingerprint existuje:

- 16 z 175 opportunities má `first_cross_at` jiný než první dochovaný
  snapshot;
- rozdíl je 35,10 až 1 125,79 minuty;
- 9 z 59 settled vyhodnocených legs je dotčeno.

**Přesná oprava**

Nepoužívat procesní sekvenční ID jako trvalou identitu. Při prvním crossing
vygenerovat UUID/ULID v databázi:

```sql
CREATE TABLE signal_episode (
    id TEXT PRIMARY KEY,
    strategy_version TEXT NOT NULL,
    market_identity_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    UNIQUE(strategy_version, market_identity_id, started_at)
);
```

Každý observation musí být append-only:

```sql
CREATE TABLE signal_observation (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES signal_episode(id),
    decision_time TEXT NOT NULL,
    benchmark_snapshot_id INTEGER NOT NULL,
    comparison_snapshot_id INTEGER NOT NULL,
    edge_model TEXT NOT NULL,
    edge REAL NOT NULL,
    eligible INTEGER NOT NULL,
    reject_reason TEXT,
    UNIQUE(
      episode_id,
      benchmark_snapshot_id,
      comparison_snapshot_id,
      edge_model
    )
);
```

Ukládání nesmí mazat existující observations. Stav episode se mění explicitním
`UPDATE ... WHERE ended_at IS NULL` v transakci. Při restartu se stav obnoví
z DB. Pokud už pro `market_identity_id + strategy_version` existuje otevřená
episode, proces ji musí použít. Novou UUID vytvoří až po prokazatelném
uzavření a novém online crossingu.

Historické `opportunity` řádky nelze bezpečně backfill opravit. Zachovat je
read-only s `data_quality = legacy_unverified`; nový experiment začít v
nových tabulkách.

**Testy**

- close → restart → recross vytvoří nové UUID a nemění staré řádky;
- restart během open obnoví stejné UUID;
- opakovaný stejný input je idempotentní;
- paralelní start dvou workerů skončí jednou otevřenou episode;
- žádná persistence operace neobsahuje `DELETE` starých observations;
- history hash staré episode se po novém crossingu nezmění.

### F-03 — `event_started` ukládá chybný čas a stale reading

Timestamp guard běžně odmítne starý nebo stejný reading, ale pro
`event_started` jej propustí. `_close()` pak reading vždy přidá do historie a
nastaví `resolved_at = reading.captured_at`.

**Reprodukce**

- opportunity otevřena v `00:00`;
- nový capture nepřibyl;
- pipeline spuštěna po kickoffu;
- tracker uzavřel opportunity jako `event_started`;
- `resolved_at` zůstalo `00:00`;
- vznikl druhý snapshot se stejným stale časem.

V release DB:

- 43 ze 44 `event_started` opportunities má `resolved_at` před současným
  kickoffem;
- průměrně o 242,6 minuty, minimum −1 535,2 minuty;
- 22 opportunities má nulovou délku, z toho 17 `event_started`;
- osm `dropped_below_threshold` řádků končí snapshotem s edge stále nad 3 %;
- jeden opportunity snapshot leží po `resolved_at`;
- v čase načtení dashboardu bylo pět open opportunities po kickoffu.

Část rozdílu může souviset s pozdější opravou event kickoffu, ale reprodukce
prokazuje samotnou chybu bez této podmínky.

**Přesná oprava**

Oddělit tři časy:

- `last_observation_at` — poslední skutečně získaný kurz;
- `state_transition_at` — čas, kdy proces provedl close;
- `kickoff_at` — event čas použitý při rozhodnutí.

Při `event_started`:

```text
ended_at = max(process_now, kickoff_at)
end_reason = event_started
```

Nesmí se přidat nový market observation, pokud nebyl získán nový snapshot.
State transition má vlastní tabulku nebo audit event; nesmí předstírat kurzové
pozorování.

Kickoff correction musí být verzovaná:

```sql
event_version(event_id, valid_from, home, away, kickoff_at, source_run_id)
```

Decision odkazuje na konkrétní `event_version_id`, aby pozdější změna
nepřepisovala historickou pravdu.

**Testy**

- close po kickoffu bez nového snapshotu nezvýší počet observations;
- `ended_at >= kickoff_at`;
- pozdější změna kickoffu nemění historické decision;
- stejný timestamp nevytvoří duplicitní observation;
- safety resolver uloží vlastní transition source a aktuální čas.

### F-04 — Method B nehledá vlastní crossing

Relevantní místa:

- `vb/evaluation.py:122–124`;
- `vb/evaluation.py:242–255`;
- `dashboard_template.html:428–433`.

Chyba a její číselný dopad jsou v §6.3.

**Přesná oprava**

Method A a Method B musí mít samostatné, verzované strategie:

```text
strategy_id = raw-v1
signal model = raw_edge
threshold = 0.03

strategy_id = proportional-v1
signal model = proportional_devig
threshold = 0.03
```

Každý fresh paired observation se vyhodnotí pro každou aktivní strategii.
Každá strategie má vlastní state machine, crossing, persistence a decision.
Evaluátor nesmí odvozovat B z A opportunities.

Dočasná oprava starého dashboardu může najít první
`opportunity_snapshot.edge_b >= threshold`, ale musí se jmenovat
`B crossing within A-captured trajectory` a přiznat neúplné pokrytí. Nesmí se
prezentovat jako úplný Method-B backtest.

**Testy**

- A crossing v `t0`, B crossing v `t2`: B entry je `t2` a používá odds z `t2`;
- A překročí, B nikdy ne: žádný B bet;
- B překročí po A close v modelu, kde je to možné: samostatný B capture jej
  zachytí;
- Python a JavaScript nad golden fixture vrátí stejný entry řádek.

### F-05 — `convergence` je look-ahead filtr

Relevantní kód:

- `dashboard_template.html:461–470`;
- threshold a entry výběr `dashboard_template.html:372–382`.

**Přesná oprava**

Nahradit zpětný filtr online potvrzovací state machine:

```text
t0: edge překročí threshold → candidate
každý fresh observation:
  pokud edge < threshold → candidate zrušit
  pokud stale/skew → candidate pozastavit nebo zrušit dle předem daného pravidla
  pokud elapsed >= 5 min a fresh sample count >= K:
      decision_at = now
      offered_odds = aktuální comparison odds
      vytvořit nejvýše jedno bet decision
```

Pro první test doporučuji `K = 2` nebo `K = 3`, podle skutečné minutové
cadence. Parametr i chování při chybějícím vzorku musí být předem zmrazené.

Dashboard může zobrazit dvě odlišné analýzy:

- `retrospective episode duration` — popis kvality signálu, bez simulovaného
  P&L;
- `online delayed-entry strategy` — skutečný decision time a tehdy dostupný
  kurz.

### F-06 — Výsledky nevznikly auditovaným kódem

Časová osa:

- DB začíná `2026-07-23T15:00:21Z`;
- první commit je `2026-07-24T08:36:36Z`;
- 23 z 59 settled Method-A legs vstoupilo před prvním commitem;
- všech 59 vstoupilo nejpozději `2026-07-25T08:51Z`;
- hlavní opravy opportunity duplicit přišly 2026-07-25 odpoledne;
- auditovaný HEAD je z 2026-07-25 18:52Z.

Commit history otevřeně uvádí:

- 35 opportunity duplicate groups / 41 duplicitních řádků;
- snapshot-level dedup;
- stuck-open safety resolver;
- opětovný výskyt same-timestamp anomálie mimo merge.

Databáze nemá `git_sha`, schema version ani strategy version u opportunity.
Současné ROI proto není výkon současného programu. Je to výsledek dat, která
prošla několika neidentifikovatelnými verzemi a reconciliation kroky.

**Přesná oprava**

Každý `capture_run`, `signal_observation`, `bet_decision`, settlement výpočet
a dashboard export musí ukládat:

- `git_sha`;
- `schema_version`;
- `strategy_version`;
- canonical JSON konfiguraci;
- hash konfigurace;
- run ID;
- vytvořeno UTC.

Dashboard musí umožnit kohortu jedné immutable `strategy_version`. Po změně
logiky se verze nesmí přepsat; začne nová kohorta.

Starý dataset označit:

```text
experiment = legacy-development
attributable_to_head = false
eligible_for_performance_claim = false
```

### F-07 — GitHub Actions cadence neodpovídá metodice

Workflow obsahuje mnoho cron výrazů, ale všechny používají:

```yaml
concurrency:
  group: vb-capture
  cancel-in-progress: false
```

GitHub concurrency drží nejvýše jeden running a jeden pending job v jedné
group; novější pending nahradí starší. Scheduled workflows navíc mohou být při
zátěži zpožděny nebo zahozeny. Viz:

- [GitHub: Control workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency);
- [GitHub: Troubleshoot delayed or dropped scheduled workflows](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows).

Auditované workflow je v `.github/workflows/capture.yml:8–41`.

Za 24 hodin končících posledním DB snapshotem:

| Metrika | Hodnota |
|---|---:|
| plánované cron firingy podle YAML | 361 |
| skutečné workflow runy | 34 |
| success | 33 |
| failure | 1 |
| medián mezery mezi starty | 35,57 min |
| maximum | 194,03 min |

DB cadence v témže okně:

| Web | Rozpoznané runy | Medián mezery | Maximum |
|---|---:|---:|---:|
| Loro | 35 | 32,25 min | 189,5 min |
| Pinnacle | 35 | 30,85 min | 197,0 min |
| Swisslos | 35 | 30,91 min | 197,1 min |

Komentář workflow, že offset schedules nebudou čekat, není pravdivý.

**Přesná oprava**

Primární řešení je VPS, ne další cron kombinace:

1. jeden dlouho běžící scheduler nebo `systemd` timer každých 60 s;
2. databázový/advisory lock proti překryvu;
3. paralelní rychlé capture requests s tvrdými timeouty;
4. zvláštní pomalý discovery job, který nevytváří signály ze stale párů;
5. centralizovaná DB s WAL a pravidelným snapshotem;
6. health heartbeat a alert, když poslední úspěšný source run zestárne;
7. pipeline se spustí jen pro konkrétní úspěšný paired run.

Krátkodobě na GitHub Actions:

- jeden schedule místo 16 překrývajících se firingů;
- nepoužívat concurrency jako náhradu fronty;
- každý run časově omezit;
- nehodnotit signály, pokud kterákoli nutná source fáze selže;
- persistovat DB před každou další riskantní fází;
- na dashboardu ukázat skutečnou cadence a source health.

GitHub nyní podporuje i frontu s `queue`, ale hromadění starých capture jobů
není řešení: stale job po hodině nevytvoří cenu, která byla dostupná v
plánovaný čas.

### F-08 — Denní handicap capture se ztratil

Run
[30146441872](https://github.com/Dexter696/value-betting-poc/actions/runs/30146441872)
proběhl přibližně 05:50–06:30Z a podle logu získal:

- 1 210 Pinnacle eventů;
- 17 449 Pinnacle snapshotů;
- 433 Swisslos eventů;
- 797 Swisslos snapshotů;
- 317/433 handicap matches;
- 938 Swisslos AH snapshotů.

Release backup poté skončil HTTP 403 `Resource not accessible by integration`.
Capture job nemá `contents: write`; workflow nastavuje page permissions až
u deploy jobu. Relevantní YAML:

- backup: `.github/workflows/capture.yml:109–113`;
- permissions pouze u deploy: `.github/workflows/capture.yml:138–151`.

Následující quick run
[30147871967](https://github.com/Dexter696/value-betting-poc/actions/runs/30147871967)
obnovil cache z běhu před handicap runem. Tím se potvrzuje, že získaná data
nepřežila. Release asset byl později vytvořen/aktualizován ručním workflow
dispatch.

**Přesná oprava**

Na stávajícím workflow:

```yaml
jobs:
  capture:
    permissions:
      contents: write
```

Samotné oprávnění ale nestačí. `actions/cache` není transakční databázové
úložiště. Po capture musí následovat trvalý upload DB artefaktu/snapshotu
ještě před reportem a deployem. Každý run musí mít unikátní artifact; pozdější
proces může atomicky označit poslední validní snapshot. Na VPS použít lokální
durable disk, SQLite backup API a off-host kopii.

Test workflow má:

1. vytvořit sentinel row;
2. simulovat chybu reportu nebo release;
3. v dalším runu ověřit, že sentinel zůstal;
4. ověřit skutečný zápis release assetu s minimálním oprávněním;
5. alarmovat, pokud daily handicap source count klesne pod očekávané minimum.

## 10. P1 nálezy

### F-09 — Překrývající se instance a chybějící bet identity

Oprava v commitu `5ccfb08` zavedla stabilní identitu
`(market_key, first_cross_at)` pro merge. Tato dvojice řeší část duplicit,
ale neřeší:

- F-02, kdy nový proces znovu použije stejné `instance_id`;
- dvě různá `first_cross_at` pro časově překrývající se trajektorie;
- otázku, zda byla sázka vůbec zamýšlena nebo provedena;
- limit „nejvýše jedna sázka na stejný event/market/selection“.

Release DB obsahuje dvě překrývající se dvojice popsané v §7.3. Obě zhoršily
headline P&L o dvě jednotky. Automatický reconciliation je nesloučil, protože
`first_cross_at` se liší.

**Oprava**

Oddělit:

- `signal_episode` — průběh signálu;
- `bet_decision` — jednorázové rozhodnutí strategie;
- `bet_execution` — skutečně přijatá sázka.

Na `bet_decision` zavést předem stanovený idempotency key:

```text
hash(
  strategy_version,
  canonical_event_id,
  market_type,
  normalized_line,
  selection,
  comparison_site,
  decision_policy_window
)
```

Pro první experiment doporučuji nejvýše jedno decision na
`event + market + line + selection + site + strategy_version`. Re-entry po
poklesu a novém crossingu povolit až jako samostatnou předem testovanou
strategii. Dashboard musí počítat executed bets, ne počet episode rows.

### F-10 — Pruning vybírá nejvyšší ID místo nejnovějšího času

`prune_raw_snapshots()` ponechá `MAX(id)` každého market key a ostatní řádky
starší než 24 hodin smaže. To předpokládá, že vyšší ID vždy znamená novější
`captured_at`. Merge však může vložit starý historický řádek později a dát mu
vyšší ID.

V release DB je 3 292 market keys, kde:

```text
captured_at(row with MAX(id)) < MAX(captured_at)
```

Rozdíl je 405 až 2 443 minut, průměr přibližně 1 028 minut. Jakmile oba řádky
zestárnou přes 24 hodin, pruning zachová starší a smaže skutečně novější.

Relevantní kód: `vb/storage.py:546–580`.

**Oprava**

Primárně raw historii během nového experimentu nemažte. Denní segmenty
exportujte do komprimovaného Parquet/object storage a uložte hash manifestu.

Pokud je retention nutná, používat pořadí:

```sql
ROW_NUMBER() OVER (
  PARTITION BY site, event_id, market_type, normalized_line
  ORDER BY captured_at DESC, id DESC
)
```

a smazat jen `row_number > 1` a `captured_at < cutoff` **po potvrzeném
archivním uploadu**. `line` se musí porovnávat NULL-safe.

Test vloží novější timestamp první, starší timestamp později, spustí prune a
musí zachovat novější čas.

### F-11 — Merge není bezeztrátový ani plně idempotentní

Kontrola `scripts/merge_databases.py` našla tyto konkrétní případy:

1. `raw_event INSERT OR IGNORE` ponechá starý destination kickoff/název a
   zahodí opravu ze source.
2. `event_match_review INSERT OR IGNORE` zahodí source `approved/rejected`,
   pokud destination už má `pending`.
3. Existující settlement se přeskočí; opravené skóre nebo lepší source se
   nepřenese.
4. Když je opportunity core shodné, `continue` přeskočí merge source-only
   snapshotů.
5. Reconciliation same-timestamp snapshots ponechá nejnižší ID bez porovnání
   obsahu. Rozdílný snapshot může zmizet.
6. Identity `(market_key, first_cross_at)` nesloučí překrývající se proudy.
7. Merge connection explicitně nezapíná `PRAGMA foreign_keys=ON`.
8. Při pravém ID collision může opakovaný merge vytvořit další variantu.

Relevantní oblasti: `scripts/merge_databases.py:228–278` a `:315–350`.

**Oprava**

Nejdříve odstranit více-master SQLite/cache model. Jeden autoritativní store
výrazně zmenší prostor chyb.

Pro nutný import:

- zapnout foreign keys před první transakcí;
- importovat přes stabilní source UUID, ne lokální autoincrement ID;
- vést `import_manifest(source_db_hash, started_at, completed_at)`;
- stejné source DB SHA odmítnout podruhé nebo provést idempotentně;
- `raw_event` verzovat, ne přepisovat;
- review stav řešit explicitní prioritou a audit logem;
- settlement correction vložit jako novou verzi, původní nesmazat;
- observations sjednotit přes stabilní UUID a content hash;
- při stejném timestampu a různém obsahu vytvořit conflict record, ne zvolit
  nižší ID;
- po importu ověřit row counts, hashes a foreign keys v téže transakci.

Nové merge property testy musí pro libovolné pořadí A+B ověřit:

```text
merge(A, B) == merge(B, A)          # semanticky
merge(merge(A, B), B) == merge(A,B) # idempotence
žádný source UUID ani content hash se neztratí
```

### F-12 — Method A neměří fair expected value

Method A předpokládá:

\[
p_{fair} = 1/O_{Pinnacle}
\]

Součet těchto pravděpodobností je ale větší než jedna. Kurzy obsahují vig.
Method A proto měří cenový poměr mezi dvěma bookmakery, ne očekávaný výnos
proti fair pravděpodobnosti.

Vstupní snapshoty 175 opportunities:

| Metrika | Průměr | Medián |
|---|---:|---:|
| Method A edge | +7,386 % | +4,688 % |
| Method B edge | −0,854 % | −1,831 % |
| benchmark overround | 8,302 % | 6,969 % |
| comparison overround | 10,129 % | 9,195 % |

Pouze 40/175 vstupů má `edge_b > 0` a 16/175 má `edge_b >= 3 %`.

Rozpad podle webu:

| Web | N | Průměr A | Průměr B | B ≥ 3 % | Průměr benchmark overround |
|---|---:|---:|---:|---:|---:|
| Loro | 50 | +5,636 % | −4,895 % | 2 | 11,141 % |
| Swisslos | 125 | +8,086 % | +0,762 % | 14 | 7,166 % |

Method A může být užitečný screening pro neobvyklý rozdíl cen, ale nemá se
jmenovat EV a nemá přímo řídit stake.

**Oprava**

- přejmenovat `raw_edge` na `price_gap_vs_benchmark`;
- odstranit z UI tvrzení, že je to fair EV;
- rozhodovat podle kalibrované fair probability a očekávaného **net**
  návratu;
- Method A ponechat jen jako feature a diagnostiku.

### F-13 — Proportional de-vig neřeší favorite–longshot bias

Komentář uvnitř `devig_proportional()` správně říká, že metoda bias
neodstraňuje. Horní module docstring naopak tvrdí, že Method B bias
„controls“. Druhé tvrzení je příliš silné.

Rozpad současných vstupů:

| Odds bucket | N | Průměr A | Průměr B | B ≥ 3 % | Průměr benchmark overround |
|---|---:|---:|---:|---:|---:|
| favorite | 20 | +6,334 % | −2,074 % | 2 | 8,681 % |
| mid | 83 | +5,013 % | −3,082 % | 4 | 8,442 % |
| longshot | 72 | +10,414 % | +2,052 % | 10 | 8,035 % |

Method-B hodnoty rostou u longshotů i po proportional normalizaci. To může
být reálný signál, ale také zbytkový favorite–longshot bias nebo stale
comparison cena. Současná data tyto možnosti nerozliší.

Clarke, Kovalchik a Ingram výslovně uvádějí, že normalizace
(multiplicative/proportional metoda) favorite–longshot bias nezohledňuje a v
jejich datasetech power metoda vyšla lépe:
[Adjusting Bookmaker's Odds to Allow for Overround](https://doi.org/10.11648/j.ajss.20170506.12).

**Oprava**

Implementovat model interface:

```python
FairProbabilityModel.fit(train_markets)
FairProbabilityModel.predict(market_snapshot) -> probabilities
```

Kandidáti:

1. proportional — baseline;
2. power — exponent se pro market zvolí tak, aby součet fair
   pravděpodobností byl jedna;
3. odds-ratio/logit shift;
4. Shin — jen pokud je vhodný pro daný market a stabilně řešitelný;
5. kalibrovaný sharp-consensus model.

Model se nesmí zvolit podle ROI současných 32 eventů. Volba proběhne na
historickém train období podle:

- log loss/Brier score vůči výsledku;
- calibration slope/intercept a reliability diagramu;
- closing-line calibration;
- stability podle odds bucketu a marketu.

Na následujícím časovém validation období se zvolí jediný model. Finální test
období zůstane nedotčené až do publikace výsledku.

### F-14 — Greedy matching je pořadově závislý a orientation není součástí dat

Matching postupně bere benchmark eventy a greedily vybírá nejlepší protějšek.
Výsledek závisí na pořadí anchorů. Neřeší globální one-to-one optimum:

```text
A má kandidáty X=0.90, Y=0.89
B má kandidáta X=0.88
```

Greedy A→X nechá B bez páru, i když A→Y + B→X je lepší celek.

Další problém je home/away orientace. Review evidence může označit swap, ale
approved páry se posílají do directional marketů bez trvalého
`orientation = same/swapped` a bez přemapování:

- HOME ↔ AWAY;
- handicap selection;
- znaménko handicap line;
- případně score při settlementu.

Relevantní kód: `vb/matching.py:82–152`.

**Oprava**

1. vytvořit canonical event identity se source links;
2. kandidáty omezit časovým oknem a soutěží;
3. skórovat názvy obou týmů v orientaci `same` i `swapped`;
4. řešit maximum-weight bipartite matching v každém
   `competition + kickoff window`;
5. uložit `orientation`, score components, model version a decision;
6. directional market povolit jen při známé orientaci;
7. při `swapped` explicitně přemapovat selection i handicap znaménko;
8. při nejasnosti abstain, nevytvářet opportunity.

Pro kalibraci je potřeba anotovaný dataset pozitivních **i negativních**
párů. Současných 64 approvals a 0 rejections nestačí.

Konkrétní parser chyba: Swisslos `_competition_name` ukončí text na první
číslici. Soutěž jako „2. Bundesliga“ tak může přijít o název, což dále snižuje
matching metadata.

### F-15 — Opportunity není sázka a chybí provedení

Současný systém ukládá cenový signál. Nezaznamenává:

- zda algoritmus vydal bet decision;
- čas odeslání;
- požadovaný stake a kurz;
- nabídnutý kurz při odeslání;
- přijatý stake a kurz;
- partial acceptance;
- odmítnutí nebo změnu odds;
- bookmaker bet ID;
- void/cancel;
- měnu a konverzi;
- expozici účtu a eventu.

Paper ROI proto předpokládá, že:

- každá opportunity šla okamžitě vsadit;
- kurz se nezměnil;
- celý stake byl přijat;
- více legs stejného eventu bylo možné a chtěné vsadit;
- nebyly žádné provozní náklady.

To je pro soft-book value betting silný optimistický předpoklad.

**Oprava**

Přidat:

```sql
CREATE TABLE bet_decision (
  id TEXT PRIMARY KEY,
  strategy_version TEXT NOT NULL,
  signal_observation_id TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  decision TEXT NOT NULL,          -- bet / skip
  reason TEXT NOT NULL,
  intended_odds REAL,
  intended_stake REAL,
  bankroll_before REAL,
  exposure_before REAL,
  idempotency_key TEXT NOT NULL UNIQUE
);

CREATE TABLE bet_execution (
  id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL REFERENCES bet_decision(id),
  requested_at TEXT NOT NULL,
  responded_at TEXT,
  status TEXT NOT NULL,            -- accepted/rejected/partial/price_changed
  requested_odds REAL NOT NULL,
  accepted_odds REAL,
  requested_stake REAL NOT NULL,
  accepted_stake REAL,
  external_bet_id TEXT,
  raw_receipt_hash TEXT
);
```

I při ručním paper testu má systém ve decision time uložit aktuální nabízený
kurz a o 5–15 sekund později znovu ověřit, zda zůstal dostupný. Konzervativní
simulace použije horší z:

- decision odds;
- verification odds;
- předem stanovený slippage haircut.

Headline report musí rozlišit:

- signal ROI;
- executable-paper ROI;
- accepted-bet ROI.

### F-16 — Selhání scraperu/pipeline nemusí selhat workflow

`scheduled_run.py` obaluje hlavní capture a pipeline fáze širokým
`except Exception`, vypíše chybu a pokračuje. Pinnacle navíc u per-league
`RequestException` pokračuje bez dostatečného strukturovaného souhrnu.

Důsledky:

- GitHub job může být zelený, i když zdroj selhal;
- pipeline se spustí po partial capture;
- poslední známá stale data vytvoří zdánlivě platný signál;
- cache neobsahuje logy, jen DB;
- dashboard nemá source health.

Capture je sekvenční Pinnacle → Swisslos → Loro. `--full-handicaps` Loro
capture vynechá, ale Loro pipeline přesto běží nad jeho starými daty.

**Oprava**

Každý běh zapisuje:

```sql
capture_run(
  id, scheduled_for, started_at, finished_at,
  git_sha, schema_version, status
)

source_run(
  id, capture_run_id, site, mode,
  started_at, finished_at, status,
  event_count, snapshot_count, http_error_count,
  error_code, error_summary
)
```

Pipeline přijímá explicitní `capture_run_id` a vyhodnotí jen povolenou
kombinaci source runů. Kritické selhání vrátí non-zero exit code. Dashboard
ukáže:

- poslední success per source;
- věk posledních fresh dat;
- počet odmítnutých stale/skew párů;
- gap v cadence;
- poslední error code.

Pomalý discovery run nesmí implicitně spustit signály z webu, který v něm
nebyl zachycen.

## 11. P2 a P3 nálezy

### F-17 — Settlement aritmetika je dobrá, evidence nestačí

Silná část projektu je oddělený settlement podle marketu, line a selection.
Quarter-line handicap se správně rozděluje na sousední celé/půlkové linie.
Všechny uložené outcome výsledky se podařilo znovu vypočítat.

Slabina je původ výsledku. `source = manual` nebo `manual:websearch` není
auditní evidence.

**Oprava**

```sql
CREATE TABLE result_evidence (
  id TEXT PRIMARY KEY,
  canonical_event_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_event_id TEXT,
  source_url TEXT,
  retrieved_at TEXT NOT NULL,
  home_goals INTEGER,
  away_goals INTEGER,
  status TEXT NOT NULL,
  raw_payload_hash TEXT,
  reviewer TEXT,
  reviewed_at TEXT
);

CREATE TABLE settlement_version (
  id TEXT PRIMARY KEY,
  settlement_key TEXT NOT NULL,
  evidence_id TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL,
  supersedes_id TEXT
);
```

Automatické zdroje mají ukládat raw response do content-addressed archivu.
Manuální oprava má vyžadovat URL, reviewer a důvod. Dashboard má ukázat
settlement coverage a podíl verified/automatic/manual.

### F-18 — Kelly je sizing hypotéza, ne důkaz edge

Kelly sám nezvyšuje očekávané ROI na jednu vsazenou korunu. Pokud je fair
pravděpodobnost správná, mění trade-off mezi růstem a rizikem. Pokud je
pravděpodobnost nadhodnocená, zvyšuje ztrátu.

Původní Kellyho kritérium maximalizuje očekávaný logaritmický růst:
[Kelly, A New Interpretation of Information Rate](https://doi.org/10.1002/j.1538-7305.1956.tb03809.x).

**Oprava**

Do doby kalibrace používat pro paper test flat stake. Po splnění datových a
modelových gate:

- nejvýše 0,10–0,25 Kelly;
- hard cap na jednu sázku;
- hard cap na jeden event;
- hard cap na souběžné korelované markets;
- hard cap na den;
- pravděpodobnost před Kellym shrinknout ke closing consensus podle
  out-of-sample kalibrační chyby;
- pro push/half outcomes numericky maximalizovat expected log return;
- bankroll simulovat chronologicky podle `execution.accepted_at`;
- reportovat max drawdown, time under water a ruin threshold.

Nejdříve se má prokázat edge flat stakingem. Sizing nesmí zakrýt chybu modelu.

### F-19 — Nevalidní varianty mohou projít jako implicitní „druhá možnost“

Příklady:

- totals settlement explicitně řeší `OVER`; jakákoli jiná selection se
  implicitně považuje za `UNDER`;
- handicap explicitně řeší `HOME`; cokoli jiného se považuje za `AWAY`;
- quarter line se zaokrouhlí přes `round(line * 4)`; neplatná hodnota blízko
  čtvrtiny se může tiše přichytit;
- parsers totals/AH dostatečně neověřují, že oba outcomes patří ke stejné
  line a mají přesně povolené opačné selections;
- `LIKE` v lookupu benchmark event ID interpoluje identifikátor; současná
  numeric ID nejsou problém, ale wildcard source ID by byl;
- SQL unique constraint s nullable `line` sám o sobě nezaručí unikátnost
  NULL-line settlementu. Aplikační workaround současná data chrání.

**Oprava**

Na hranici scraperu validovat canonical market:

```text
MATCH_WINNER: selections přesně HOME, DRAW, AWAY; line IS NULL
TOTALS: selections přesně OVER, UNDER; stejná line; line v povoleném kroku
ASIAN_HANDICAP: HOME, AWAY; lines jsou správně orientované a normalizované
```

Každá větev nad enumem musí explicitně obsloužit všechny platné hodnoty a
jinak vyhodit chybu. DB přidá CHECK constraints a NULL-safe expression index.
Property testy pokryjí celé/půl/čtvrt line a neplatné selection kombinace.

Finanční matematiku je vhodné ukládat jako integer basis points / decimal
string nebo používat `Decimal` na výpočtové hranici. Současné float rozdíly
nezpůsobily nalezený P&L nesoulad, ale validace line nemá spoléhat na přibližné
zaokrouhlení.

### F-20 — Build není přesně reprodukovatelný

`requirements.txt`:

```text
rapidfuzz
unidecode
pytest
playwright
requests
```

Chybí:

- verze a hashes;
- oddělení runtime/dev;
- `tzdata` pro platformy bez systémové timezone DB;
- deklarace podporované Python verze;
- lockfile;
- automatická dependency/security kontrola.

**Oprava**

- definovat `pyproject.toml`;
- oddělit `project.dependencies` a `optional-dependencies.dev`;
- vytvořit hashovaný lockfile;
- CI testovat podporovanou produkční verzi Pythonu;
- zahrnout timezone data nebo výslovně vyžadovat OS package;
- Playwright browser instalovat v dedikovaném kroku;
- přidat Ruff/type check jen s pravidly, která tým chce udržovat.

85 Ruff nálezů není samo o sobě funkční chyba. Důležitější jsou široké
`except Exception`, implicitní enum fall-through a netestované integrační
větve.

### F-21 — Pre-entry graf není historický replay rozhodnutí

`pre_entry_history`:

- iteruje benchmark timestamps;
- k nim bere poslední comparison snapshot `<= benchmark timestamp`;
- znovu provádí fuzzy matching;
- přijímá jen auto match, ne lidsky approved review;
- neodkazuje na původní comparison event ID;
- pracuje jen s raw řádky, které přežily pruning.

Skutečný capture běžel sekvenčně a pipeline používala poslední známý snapshot
obou webů po dokončení běhu. Graf tedy nekreslí prokazatelně stejný pár
snapshotů, nad kterým vzniklo rozhodnutí. Je to rekonstrukce.

**Oprava**

Každý `signal_observation` musí odkazovat na oba konkrétní raw snapshot IDs a
konkrétní `event_match_id`. Dashboard pak historii kreslí jen z uložených
vazeb. Rekonstrukce ze starých raw dat se označí `estimated_replay` a nesmí se
míchat s decision audit trail.

### Další P3 nesoulady

- komentář schématu tvrdí, že `full_market_json` obsahuje všechny čtyři weby;
  produkční JSON obsahuje jen benchmark a comparison pair;
- dokumentace uvádí v jedné mapě pět tabulek, schéma jich má šest;
- některé popisy normalizace slibují kontrolu favorite–longshot bias, kterou
  proportional metoda neposkytuje;
- forced resolver je v dokumentaci odlišen od normálního close, ale normální
  event-start cesta sama používá chybný captured time;
- dashboardové headline tiles neukazují počet unikátních eventů vedle legs.

Tyto položky opravit spolu se změnou datového modelu. Dokumentace má být
generována nebo testována proti schema/strategy metadata tam, kde je to
možné.

## 12. Hodnocení kvality kódu

### Co je uděláno dobře

- Moduly mají rozumně oddělené oblasti: scraping, normalization, matching,
  edge, lifecycle, storage, settlement a evaluation.
- Doménové dataclasses/enums zjednodušují testy.
- Edge a settlement funkce jsou malé a deterministické.
- Quarter-line settlement je věcně správně rozdělen na dvě poloviny.
- Vývojová historie otevřeně popisuje nalezené chyby a důvody oprav.
- Repozitář má na velikost POC slušný test suite; 146 testů prochází.
- Release DB projde SQLite integrity a foreign-key kontrolou.
- Dashboard používá stejnou základní profit aritmetiku jako Python.
- Projekt odděluje raw capture od pozdějšího settlementu lépe než jednorázový
  scraper bez historie.

### Co je konstrukčně slabé

- Procesní in-memory stav se používá pro trvalou identitu.
- Persistence přepisuje agregovaný objekt a maže jeho děti místo append-only
  event logu.
- „Latest“ nahrazuje explicitní run/time pair.
- Capture, decision, execution a evaluation nemají samostatné entity.
- GitHub cache a release asset suplují autoritativní databázi.
- Chybové větve pokračují nad stale stavem.
- Merge se snaží rekonstruovat distribuovanou historii bez stabilních UUID.
- Reportovací filtr mění význam simulace.
- Verze strategie a kódu není součástí dat.
- Scraper úspěch není datový předpoklad pipeline.

### Pokrytí testy

Celkových 84 % line coverage působí dobře, ale průměr zakrývá integrační
riziko. Nejnižší coverage mají:

- Loro scraper: přibližně 68 %;
- Pinnacle scraper: přibližně 70 %;
- Swisslos scraper: přibližně 55 %;
- results fetching: přibližně 55 %.

Před auditem chyběly testy pro:

- close/restart/recross;
- stale source a skew;
- event-start bez nového snapshotu;
- nejnovější čas vs nejvyšší ID při prune;
- source-only snapshots při merge;
- vlastní Method-B crossing;
- online delayed entry;
- celý workflow po selhání durable backup.

Test coverage proto nepředstavuje důkaz správnosti pipeline jako celku.

## 13. Může základní algoritmus fungovat?

### 13.1 Hypotéza

Základní hypotéza dává smysl:

1. likvidnější nebo rychlejší trh zpracuje novou informaci dříve;
2. pomalejší bookmaker dočasně nabídne starý kurz;
3. pokud sharp cena po odstranění marginu dobře odhaduje pravděpodobnost a
   soft kurz lze skutečně přijmout, vznikne kladná očekávaná hodnota.

Systém však musí současně splnit všechny podmínky:

\[
\text{fresh data}
\land \text{správný event/market/selection}
\land \text{kalibrovaná fair probability}
\land \text{dostupný kurz}
\land \text{přijatelný stake}
\land \text{edge po nákladech}
\]

Současný POC bezpečně nezajišťuje žádnou z těchto podmínek kromě základní
aritmetiky edge pro zadané odds.

### 13.2 Pinnacle není automaticky „pravda“

Pinnacle lze použít jako silný informační vstup, ale publikovaný kurz:

- obsahuje margin;
- může být stale v konkrétním capture;
- má rozdílnou kvalitu podle marketu, soutěže, času do kickoffu a limitů;
- není totožný s closing probability;
- může nést vlastní favorite–longshot bias;
- nevyjadřuje nejistotu modelu.

Lepší fair probability proto vychází z časově synchronizovaného sharp
consensu a následné kalibrace, ne z jednoho vigged outcome.

### 13.3 Co současný výsledek skutečně říká

Data ukazují:

- systém našel velké rozdíly cen;
- Swisslos podmnožina v tomto malém vzorku vyšla kladně;
- Loro vyšla záporně;
- Method A a Method B vybírají výrazně jiné množiny;
- velká část Method-A edge zmizí po prostém odstranění overroundu;
- výsledky jsou citlivé na lifecycle a filtr;
- intervaly zahrnují velkou ztrátu i velký zisk.

Data neukazují:

- že rozdíly byly ve stejný okamžik obchodovatelné;
- že fair pravděpodobnost byla správná;
- že soft kurz zůstal dostupný po rozhodnutí;
- že sázka by byla přijata;
- že ROI přežije out-of-sample období;
- že jedna z dashboardových podskupin má stabilní edge.

### 13.4 Odpověď

**Koncept může fungovat. Současná implementace ani dataset zatím neprokazují,
že funguje tato konkrétní strategie.** Největší šance na lepší skutečné ROI
není ve změně Kellyho koeficientu nebo v nalezení nejlepšího slideru. Je v
odstranění falešných signálů, lepším fair-price modelu a měření
proveditelného kurzu.

## 14. Změny s potenciálem zlepšit skutečné ROI

Žádná změna ROI nezaručí. Tabulka rozlišuje zvýšení správnosti měření od
hypotézy o vyšším budoucím výnosu.

| Pořadí | Změna | Co řeší | Očekávaný dopad | Jak ověřit |
|---:|---|---|---|---|
| 1 | freshness/skew/source-success gate | falešné stale rozdíly | méně signálů; přesnější, možná vyšší realized ROI | nový paper A/B podle předem daných limitů |
| 2 | decision/execution log | nedostupné kurzy a limity | reportované ROI pravděpodobně klesne, ale bude reálné | accepted-odds ROI a rejection rate |
| 3 | sharp consensus + kalibrovaný de-vig | chybná fair probability | potenciálně méně false positives | time-split log loss, calibration, CLV a test ROI |
| 4 | net-EV threshold s error bufferem | edge menší než model/slippage chyba | nižší objem, lepší margin of safety | předem zmrazený walk-forward |
| 5 | samostatný online persistence model | krátké price blips | neznámý; současný filtr jej netestuje | rozhodnutí až po čekání za aktuální odds |
| 6 | event/market exposure a dedup | více korelovaných bets | nižší variance a menší tail loss | event-cluster P&L/drawdown |
| 7 | closing-line validation | slabý nebo stale signál | rychlá diagnostika před settlementem | out-of-sample CLV |
| 8 | skutečná arbitrage jako zvláštní strategie | model risk | menší nominální edge, vyšší jistota ceny | executable all-leg quotes a fill log |
| 9 | market/site segmentace | rozdílná kvalita zdrojů | možný růst ROI, vysoké overfit riziko | až na dost velkém train/validation vzorku |
| 10 | fractional Kelly po kalibraci | růst vs drawdown | nemění edge ani ROI/stake | chronologická bankroll simulace |

### 14.1 Freshness, skew a refetch benchmarku

Toto je první změna, protože bez ní nelze interpretovat žádný model. U každého
signálu logovat:

- věk obou quotes;
- absolutní skew;
- request latency;
- capture status;
- čas do kickoffu;
- zda benchmark po comparison capture změnil cenu.

Pokud full comparison sweep trvá osm minut, benchmark ze začátku sweepu se
nesmí použít proti výsledku z jeho konce. Buď:

- fetch proběhne paralelně pro konkrétní páry;
- nebo se benchmark po sweepu refetchne;
- nebo se použije časově nejbližší benchmark a signál nad max skew se odmítne.

Nejdříve změřit distribuci latency a teprve poté potvrdit limity. Limity se
nesmí uvolnit jen proto, aby vzniklo více bets.

### 14.2 Sharp consensus a fair probability

Navržený postup:

1. sbírat alespoň dva nezávislé sharp zdroje a případně exchange;
2. normalizovat tentýž event, market, line a selection;
3. každý market de-vigovat více předem danými metodami;
4. vytvořit consensus v logit prostoru, vážený historickou kalibrací a
   likviditou/limitem;
5. model kalibrovat jen na minulém train období;
6. model version zmrazit;
7. testovat na následujícím období.

Betfair, požadovaný původní metodikou, může dodat exchange cenu a likviditu.
Je nutné pracovat s executable back/lay odds, dostupným objemem a komisí.
Pouhý midpoint bez likvidity není proveditelná cena.

Příklad modelového výstupu:

```text
p_mean
p_lower
model_version
source_count
source_dispersion
calibration_bucket
```

Rozhodnutí pro binární full-win/full-loss market:

\[
EV_{net} = p_{mean} O_{expected\_accept} - 1 - c
\]

kde \(c\) zahrne měřitelné náklady. Vstup se povolí jen pokud konzervativní
hodnota:

\[
p_{lower} O_{expected\_accept} - 1 - c > 0
\]

`p_lower` se nemá zvolit libovolně. Má vycházet z out-of-sample kalibrační
chyby daného marketu/odds bucketu.

U Asian handicapu a quarter-line nelze vždy použít jedinou binární
pravděpodobnost. Model musí odhadnout distribuci score difference nebo přímo
pravděpodobnosti návratových stavů a spočítat:

\[
EV = \sum_s p_s r_s
\]

### 14.3 Power de-vig jako první challenger

Power metoda je malá změna proti současnému kódu a vhodný první challenger:

\[
p_i = q_i^k
\]

kde \(k\) se numericky zvolí tak, aby:

\[
\sum_i q_i^k = 1
\]

Implementace:

- nový `vb/fair_probability.py`;
- čisté funkce `proportional`, `power`, `odds_ratio`, případně `shin`;
- solver s jasnou tolerancí a hard failure mimo validní trh;
- model ID a parametry v každém observation;
- unit testy pro 2-way a 3-way market;
- property test: každé \(p_i \in (0,1)\) a součet je v toleranci jedna;
- golden fixture proti nezávislému výpočtu;
- time-split model comparison, ne ROI optimalizace na současné DB.

Power metoda není automaticky nejlepší. Musí vyhrát předem určenou
out-of-sample metriku.

### 14.4 Closing-line value

Pro každé executed-paper decision zachytit poslední fresh sharp consensus před
kickoffem na stejné line. Definovat například:

\[
CLV_{fair} = O_{accepted} \cdot p_{close} - 1
\]

Výhody:

- je znám krátce po kickoffu, ne až po settlementu;
- má větší sample než settled P&L při dlouhých soutěžích;
- odhalí, zda strategie soustavně bere lepší cenu než pozdější sharp trh;
- pomůže rozlišit pricing signal od náhodného score výsledku.

CLV není náhrada za profit a closing line není absolutní pravda. Je to
diagnostická metrika. Před experimentem určit:

- přesný closing timestamp;
- zdroje consensu;
- de-vig metodu;
- postup při chybějící line;
- event-cluster interval.

### 14.5 Online persistence

Současný post-hoc convergence výsledek neříká, zda čekání zvyšuje ROI.
Vytvořit dvě paralelní, předem definované paper strategie:

- `immediate-v1`: vstup na prvním fresh net-EV crossingu;
- `persistent-5m-v1`: vstup po pěti minutách a nejméně třech fresh
  observations, pokud net edge nepřerušeně zůstala nad prahem.

Obě používají svůj skutečný decision kurz. Stejný signal lze logovat pro obě,
ale každá má oddělený decision a exposure. Porovnat až po dosažení předem
určeného počtu eventů.

### 14.6 Execution-aware edge

Soft-book price se může změnit mezi scrape a betslipem. Modelovat:

```text
quoted_odds
verified_odds
accepted_odds
price_change_bps
requested_stake
accepted_stake
response_latency
rejection_reason
```

Z historical paper execution logu odhadnout:

- pravděpodobnost akceptace podle edge/site/market/latency;
- distribuci odds slippage;
- stake fill ratio.

Rozhodovací expected value pak použije očekávaný přijatý kurz, ne první
scraped kurz. Reporting musí uvádět dvě denominator definice:

- profit per accepted stake;
- profit per signal včetně nulového profitu neprovedených signálů.

### 14.7 Expozice a korelace

Jedna informace může současně vytvořit:

- home/away price rozdíl;
- match winner i handicap;
- více comparison sites;
- opakovaný crossing.

To nejsou nezávislé bets. Policy musí před rozhodnutím počítat expozici podle:

- canonical event;
- týmu;
- marketu a line;
- comparison účtu;
- času.

První bezpečná verze:

- nejvýše jedna selection stejného marketu na event a site;
- nejvýše jedna episode re-entry;
- event cap;
- korelované bets sdílejí event cap;
- protichůdné positions explicitně zakázat nebo označit jako arbitrage bundle.

### 14.8 Arbitrage jako oddělená strategie

Pokud se ze současných zdrojů složí executable odds pro všechny vzájemně
výlučné outcomes a:

\[
\sum_i \frac{1}{O_i} < 1
\]

existuje teoretická arbitrage. Ta má jiný algoritmus než value betting:

- musí zachytit všechny legs téměř současně;
- musí znát dostupné limity;
- musí řešit pořadí fillů a riziko, že druhá leg zmizí;
- musí zahrnout exchange commission;
- musí přesně sjednotit pravidla void/extra-time/handicap settlementu.

Nesmí se míchat do Method A/B výsledků. Pokud je provedení pouze ruční,
arbitrage může být v praxi neproveditelná kvůli latenci.

### 14.9 Segmentace webu, marketu a odds

Současná data svádějí k pravidlu „Swisslos ano, Loro ne“ nebo „longshot
ne“. Takové pravidlo by bylo vybrané podle výsledků stejného vzorku.

Segment povolit až tehdy, když:

1. existuje ekonomický důvod známý před testem;
2. má dostatečný train a validation vzorek;
3. efekt je stabilní v čase;
4. drží po odečtení slippage;
5. test period nebyla použita k výběru.

Odds bucket má být feature pro kalibraci favorite–longshot bias, ne ručně
posouvaný profit slider.

### 14.10 Co ROI nezlepší

- Vybrat z aktuální tabulky threshold 7,5 %.
- Vyřadit Loro jen proto, že v prvních 20 bets prodělalo.
- Zvýšit Kelly na malé Method-B kohortě.
- Po každém výsledku upravit pravidlo a přepočítat celou historii.
- Přidat více stale capture jobů do fronty.
- Počítat legs jako nezávislé pozorování.
- Opravit datové anomálie způsobem, který maximalizuje staré ROI.

Tyto kroky zlepší in-sample číslo, ne důkaz očekávaného výnosu.

## 15. Cílový návrh systému

### 15.1 Tok

```text
              ┌─────────────────────────┐
              │ Scheduler / capture_run │
              └────────────┬────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
     Pinnacle          Exchange        Comparison books
          │                │                │
          └──── source_run + immutable market_snapshot ────┐
                                                            ▼
                                          canonical event/market mapping
                                                            │
                                                            ▼
                                               time-pair eligibility gate
                                                            │
                                                            ▼
                                         fair-probability model + net EV
                                                            │
                         ┌──────────────────────────────────┴────────┐
                         ▼                                           ▼
                 signal_observation                         rejected observation
                         │
                         ▼
                  online strategy state
                         │
                         ▼
                    bet_decision
                         │
                         ▼
                    bet_execution
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
       closing snapshot        result evidence
             │                       │
             └──────── evaluation / settlement ────────┐
                                                       ▼
                                          version-scoped dashboard
```

### 15.2 Navržené entity

| Entita | Účel |
|---|---|
| `capture_run` | jeden plánovaný/ruční běh, verze a stav |
| `source_run` | výsledek konkrétního scraperu |
| `event_version` | zdrojová identita a verzovaný kickoff/název |
| `canonical_event` | stabilní zápas napříč zdroji |
| `event_match` | source link, orientation, score, review, model version |
| `market_snapshot` | immutable cena s runem a časy |
| `signal_observation` | přesný pár snapshotů, model, edge a reject reason |
| `signal_episode` | online souvislá trajektorie |
| `strategy_definition` | immutable config + hash |
| `bet_decision` | přesně jedno rozhodnutí a intended stake |
| `bet_execution` | accepted/rejected cena a stake |
| `closing_snapshot` | předem definovaná closing reference |
| `result_evidence` | původ finálního skóre |
| `settlement_version` | verzovaný výpočet výsledku |
| `evaluation_run` | kód/config/data hash a výstupní metriky |

### 15.3 Invarianty

Databáze a testy musí vynutit:

1. raw market snapshot se po vložení nemění;
2. observation vždy odkazuje na dva existující snapshoty;
3. oba snapshoty splnily source success, age a skew limit dané strategie;
4. directional market má známou orientation;
5. strategy definition je immutable;
6. decision má unikátní idempotency key;
7. execution nikdy neexistuje bez decision;
8. settlement odkazuje na evidence;
9. oprava výsledku vytvoří novou verzi;
10. evaluation uvádí přesný data cutoff, strategy version a code SHA;
11. jeden dashboard headline nikdy nemíchá různé strategy versions;
12. žádný backfill nevytvoří předstíraný offered/accepted kurz.

### 15.4 SQLite nebo PostgreSQL

Pro jeden VPS proces může SQLite v WAL režimu stačit, pokud:

- existuje jeden writer;
- používá transakce;
- backup běží přes SQLite backup API;
- více capture workers posílá data jednomu writeru;
- merge z více masterů skončí.

PostgreSQL dává smysl při více workerech, API a souběžném dashboardu. Samotná
změna databáze ale neopraví časovou synchronizaci ani model. Pro další POC je
jednodušší jeden autoritativní SQLite writer na VPS než GitHub cache.

## 16. Co ukazuje historie vývoje

Repozitář vznikl a výrazně se změnil během přibližně 34 hodin. Pořadí commitů:

| Období | Změny | Auditní význam |
|---|---|---|
| 24. 7. 10:36–14:07 CEST | první pipeline, Actions, Loro diagnostika, full Swisslos, max bet, ESPN | data už existovala před prvním commitem; rychlé rozšíření zdrojů |
| 24. 7. 14:37–17:23 | Kelly, Swisslos AH, Pages dashboard, simulátor, DB merge, release backup | reporting a distribuce přibyly dříve než stabilní provenance model |
| 24. 7. 20:51–22:30 | první merge duplicate fix, threshold UI, Kelly popis, zero-duration fix | první doložené problémy s deduplikací a metrikami |
| 25. 7. 16:18–16:53 | settlement backlog, 35 duplicate groups, snapshot dedup, stale-open resolver, dokumentace | velké opravy přišly až po všech auditovaných settled entry časech |
| 25. 7. 17:42–20:52 | convergence filtr a opravy, audit dokumentace, recurrence same-timestamp anomaly | UI filtr a datové anomálie se ještě měnily těsně před externím auditem |

Pozitivní je, že commit messages chyby netají a popisují rationale. Negativní
důsledek je, že release DB je směs:

- předrepozitářových dat;
- více verzí algoritmu;
- merge a cleanup výstupů;
- řádků bez code/strategy provenance.

Historii proto nelze hodnotit jako jeden neměnný experiment. Je to vývojový
dataset použitý zároveň k ladění a měření. Nový experiment musí začít až po
zmrazení v2 schématu a strategie.

## 17. Implementační roadmap

### Fáze 0 — Zmrazit starý experiment

**Cíl:** zabránit dalšímu míchání vývojových dat s budoucí validací.

Kroky:

1. Release DB uložit read-only pod názvem s datem, SHA-256 a HEAD commitem.
2. Dashboard označit `Legacy development data — not attributable to current
   strategy`.
3. Odebrat z headline tvrzení, která Method B nebo pětiminutový filtr
   prezentují jako samostatný proveditelný výsledek.
4. Zachovat aktuální report pro historické srovnání, ale žádný řádek
   „opravou“ nepřepisovat.
5. Založit `experiment_id = legacy-development-2026-07`.

Dotčené soubory:

- `dashboard_template.html`;
- `scripts/build_dashboard.py`;
- `PROJECT_DOCUMENTATION.md`.

Podmínka přijetí:

- starý dashboard a DB mají zobrazený hash/cutoff;
- nejsou součástí headline nového experimentu.

### Fáze 1 — Schéma v2 a append-only persistence

**Cíl:** každé rozhodnutí lze zpětně vysvětlit bez rekonstruování.

Kroky:

1. V `vb/schema.sql` přidat entity z §15.2 a `schema_migration`.
2. V `vb/models.py` vytvořit typy `CaptureRun`, `SourceRun`,
   `MarketSnapshotV2`, `SignalObservation`, `StrategyDefinition`,
   `BetDecision`, `BetExecution`.
3. Ve `vb/storage.py` zavést insert-only metody a transakční state
   transitions.
4. Odstranit z nové cesty delete/rewrite opportunity snapshots.
5. Každému objektu dát UUID/ULID vytvořené při vzniku, ne při merge.
6. Zavést canonical serialization a config hash.
7. Vytvořit `scripts/migrate_schema_v2.py`.

Migrační pravidla:

- staré raw rows lze importovat s `quality = legacy_no_run_provenance`;
- staré opportunity rows se nepřevádějí na v2 `bet_decision`;
- jejich timestampy se neopravují odhadem;
- settlement lze importovat jako `legacy evidence`, ne `verified`;
- nový experiment začne na přesném `cutover_at`.

Testy:

- migrace prázdné i existující DB;
- opakovaná migrace je no-op;
- foreign keys a CHECK constraints;
- append-only trigger/test;
- restart/recross scénáře F-02/F-03;
- config hash stabilita.

Podmínka přijetí:

- starý history hash se po libovolném novém runu nemění;
- každý v2 observation má run, oba snapshots, strategy a event match.

### Fáze 2 — Spolehlivý capture na VPS

**Cíl:** minutová cadence a měřitelný stav zdrojů.

Kroky:

1. Přesunout autoritativní capture mimo GitHub Actions.
2. V `scripts/scheduled_run.py` rozdělit discovery, price capture, signal
   evaluation, settlement a export na samostatné exit-code kroky.
3. Scrapers vrací strukturovaný `SourceRunResult`, ne jen řádky/výpis.
4. Rychlé source requesty spouštět souběžně pro tentýž event set.
5. Nastavit connect/read/total timeout a retry budget.
6. Po dlouhém comparison sweepu refetchnout benchmark.
7. Zapsat source run status i při nule řádků.
8. Spustit pipeline jen nad explicitním run/párem.
9. Přidat heartbeat, disk-space kontrolu a alert.
10. Každou hodinu SQLite online backup, denně off-host komprimovaný snapshot
    s hash manifestem.

Krátkodobá oprava `.github/workflows/capture.yml`:

- `contents: write` u capture jobu;
- jeden jasný cron;
- artifact po capture;
- žádné vyhodnocení po failed source;
- dashboard deploy nesmí rozhodnout o trvanlivosti DB.

Podmínka přijetí během sedmidenního burn-in:

- ≥99 % plánovaných minutových cyklů dokončeno;
- p95 capture interval ≤90 sekund pro rychlé zdroje;
- žádný gap >5 minut bez alertu;
- 100 % source runů má stav a counts;
- nula signálů po failed source;
- nula ztracených rows po simulované chybě reportu/deploye;
- restore z off-host backupu projde integrity kontrolou.

### Fáze 3 — Canonical event/market matching

**Cíl:** directional price se nikdy nepřiřadí opačnému týmu nebo jiné line.

Kroky:

1. Opravit Swisslos competition parser.
2. Zavést `event_version` a `canonical_event`.
3. Generovat same/swapped kandidáty s komponentami skóre.
4. Použít globální bipartite assignment v časových blocích.
5. Uložit orientation a mapping každé selection/line.
6. Sestavit anotovaný dataset positives i negatives.
7. Kalibrovat auto threshold; mezní případy poslat do review.
8. Approved review ukládat verzovaně a používat v pipeline i historii.

Dotčené soubory:

- `vb/normalize.py`;
- `vb/matching.py`;
- `vb/pipeline.py`;
- scrapers;
- nové `vb/market_mapping.py`;
- review UI/script.

Podmínka přijetí:

- 100 % paper bet decisions má explicitní orientation;
- nulová directional chyba v ručně auditovaném decision setu;
- auto match reportuje precision/recall na odděleném labeled test setu;
- žádná nejasná dvojice nevytvoří bet.

Pro první paper fázi je bezpečnější vyžadovat lidsky approved event match,
pokud auto dataset ještě nemá dost negativních příkladů.

### Fáze 4 — Fair probability a net-EV strategie

**Cíl:** rozhodovat podle kalibrovaného očekávaného návratu.

Kroky:

1. Přidat `vb/fair_probability.py`.
2. Implementovat proportional baseline, power challenger a odds-ratio.
3. Přidat ostrý druhý benchmark/exchange včetně liquidity a commission.
4. Vytvořit time-aligned feature dataset.
5. Měřit log loss, Brier, calibration a source dispersion.
6. Zvolit model jen na train/validation období.
7. Zmrazit `strategy_definition` včetně age/skew/lead-time/threshold.
8. V `vb/pipeline.py` nahradit Method-A trigger obecným strategy runnerem.
9. Method A ponechat jako diagnostickou feature.
10. Pro AH použít návratovou distribuci, ne binární aproximaci.

Podmínka přijetí:

- fair probabilities se sčítají na 1 v toleranci;
- model má stabilní out-of-sample kalibraci podle předem daných bucketů;
- žádná test-period informace nebyla použita k jeho výběru;
- každý edge výpočet lze reprodukovat z immutable inputs a model version.

### Fáze 5 — Online entry a execution model

**Cíl:** simulovaný kurz odpovídá okamžiku, kdy strategie skutečně rozhodla.

Kroky:

1. Implementovat `immediate-v1` a `persistent-5m-v1` jako oddělené state
   machines.
2. Ukládat každý observation, přechod a reject reason.
3. Vygenerovat idempotentní `bet_decision`.
4. Znovu ověřit kurz po realistické ruční/automatické latenci.
5. Uložit paper `bet_execution`.
6. Zavést event/site exposure policy.
7. Zavést accepted-odds a conservative-slippage P&L.
8. Sbírat closing consensus.

Dotčené soubory:

- `vb/opportunity.py` nahradit/omezit novým `vb/strategy.py`;
- `vb/pipeline.py`;
- `vb/storage.py`;
- nové `vb/execution.py`, `vb/exposure.py`, `vb/closing.py`.

Podmínka přijetí:

- žádné decision nepoužívá quote před koncem čekacího pravidla;
- jedno decision na idempotency key;
- každý paper bet má verified odds nebo stav rejected;
- headline ROI používá accepted/verified odds;
- retrospektivní duration není v decision policy.

### Fáze 6 — Settlement evidence a evaluace

**Cíl:** report je deterministický, auditovatelný a statisticky platný.

Kroky:

1. Přidat evidence/version schéma z F-17.
2. Každý source response archivovat a hashovat.
3. Settlement code zpřísnit explicitní validací.
4. `vb/evaluation.py` přepsat nad executed bets jedné strategy version.
5. Přidat event-level counts, cluster intervals, CLV, slippage, rejection
   rate, drawdown a exposure.
6. Vygenerovat jeden JSON evaluation artifact jako autoritu pro Python i
   dashboard.
7. JavaScript už jen renderuje JSON; nepřepočítává jinou cohort logiku.
8. `evaluation_run` uloží code SHA, config hash, DB snapshot hash a cutoff.

Podmínka přijetí:

- Python/JS golden parity;
- 100 % settled bets má evidence nebo je viditelně `unverified`;
- součet row-level P&L přesně odpovídá headline;
- legs i unique events jsou vždy zobrazené;
- CI clusteruje podle canonical event;
- dashboard nedovolí vydávat exploratory filtr za confirmatory výsledek.

### Fáze 7 — Nový předregistrovaný paper experiment

Před prvním decision uložit neměnný protokol:

- experiment ID;
- počáteční a koncové pravidlo;
- aktivní strategy versions;
- source list;
- freshness/skew/lead-time;
- fair model;
- entry threshold a persistence;
- execution latency/haircut;
- exposure a stake;
- primary a secondary metriky;
- sample/end rule;
- pravidla pro incident a změnu verze.

Každá změna po startu ukončí starou strategy cohort a založí novou. Nesmí
zpětně přepočítat staré decisions jako by používaly nové pravidlo.

## 18. Testovací a validační protokol

### 18.1 Primární metrika

Primární metrika nového experimentu:

```text
flat net ROI z verified/accepted paper executions
clusterovaná podle canonical event
po předem daném slippage a všech známých nákladech
```

Flat stake oddělí kvalitu signálu od sizingu. Kelly je sekundární simulace.

Každý denominator musí být explicitní:

- počet signals;
- počet decisions;
- počet verified offers;
- počet accepted-paper executions;
- staked units;
- unikátní eventy.

### 18.2 Sekundární metriky

- mean a median `CLV_fair`;
- 95% event-cluster interval CLV;
- quote verification rate;
- acceptance/fill rate;
- odds slippage;
- source freshness/skew reject rate;
- profit podle předem daného site/market/odds členění;
- maximum drawdown;
- event exposure;
- matching review/error rate;
- settlement evidence coverage.

Segmentové ROI je exploratory, pokud protokol předem neurčí jinak.

### 18.3 Časové dělení

Náhodný train/test split je nevhodný: sousední snapshoty a eventy jsou
časově závislé.

Použít:

1. **burn-in:** provozní data, žádné ROI tvrzení;
2. **train:** výběr/calibrace fair modelu;
3. **validation:** jedna volba modelu a parametrů;
4. **test:** neměnný budoucí interval;
5. volitelně rolling walk-forward s verzemi, ale nikdy nepřepisovat minulost.

Event se celý zařadí podle decision time do jednoho období. Všechny legs
eventu zůstávají spolu.

### 18.4 Minimální gate pro závěr

Navržený test končí podle pravidla uloženého předem, ne při prvním kladném
dashboardu:

- alespoň 8 týdnů;
- alespoň 500 verified/accepted paper bets;
- alespoň 200 unikátních eventů;
- maximum 6 měsíců; pokud minima nevzniknou, závěr je „nedostatek dat“;
- po startu test období žádná změna strategie.

Pro tvrzení „data podporují kladné net ROI“ musí současně platit:

1. dolní mez předem zvoleného 95% event-cluster intervalu net ROI je nad
   nulou;
2. dolní mez 95% event-cluster intervalu CLV je nad nulou;
3. výsledek drží po conservative slippage;
4. nula P0 data-integrity incidentů v test cohort;
5. 100 % decisions má úplnou provenance;
6. nejméně 99 % settlementů je evidence-backed; zbytek je oddělen a test
   citlivosti jej vyloučí;
7. žádný post-hoc filtr neurčuje primary výsledek.

Čísla 500/200 nejsou záruka dostatečné síly. Jsou minimální provozní gate.
Rozhodující je šířka intervalu. Po burn-in pilotu je vhodné před testem
spočítat potřebný sample z event-level variance a požadované přesnosti.

### 18.5 Multiple testing

Dashboard může dál nabízet průzkum, ale musí oddělit:

- **confirmatory view:** jediná předregistrovaná primary cohort;
- **exploratory view:** libovolné filtry s viditelným varováním.

Pokud se má potvrzovat více strategií, předem určit:

- počet hypotheses;
- primary metriku;
- family-wise/FDR korekci;
- nebo samostatné budoucí test období pro každou změnu.

Výběr nejlepšího thresholdu a jeho vyhodnocení na stejných datech není
validní test.

### 18.6 Incident protocol

P0 incident:

- stale/skew decision;
- ztracený capture;
- history overwrite;
- chybný event/selection orientation;
- neidentifikovatelná code/strategy version;
- dashboard cohort mismatch.

Postup:

1. pozastavit nové decisions;
2. uložit incident, rozsah a první/poslední dotčený čas;
3. nemažte ani nepřepisujte původní data;
4. opravit kód a zvýšit strategy/schema version;
5. znovu spustit burn-in;
6. test cohort po incidentu nepovažovat automaticky za pokračování stejného
   experimentu.

### 18.7 Gate před reálnými penězi

Tento audit nedoporučuje real-money provoz. Pokud nový paper test splní výše
uvedené podmínky, další fáze má stále začít:

- malým pevným stake, ne Kellym;
- s denním/event/account limitem;
- s ruční kontrolou event/market mappingu;
- s kill switchem pro stale data, error rate a nečekaný drawdown;
- s odděleným vyhodnocením paper quote versus accepted bet;
- s právním a smluvním posouzením automatizace a podmínek bookmakerů.

Kelly nebo vyšší stake přichází až po kalibraci accepted-bet dat, ne po
současném POC výsledku.

## 19. Přesná mapa změn v repozitáři

| Soubor/oblast | Změna |
|---|---|
| `vb/schema.sql` | v2 run/provenance/decision/execution/evidence tabulky, constraints a indexes |
| `vb/models.py` | immutable IDs, run a strategy typy, explicitní enum varianty |
| `vb/storage.py` | append-only inserts, transakční transitions, time-based retention |
| `vb/pipeline.py` | explicitní capture run, freshness/skew gate, strategy runner |
| `vb/opportunity.py` | nahradit procesní `#N` tracker persistentní episode state machine |
| `vb/edge.py` | přejmenovat raw gap; model interface místo jediného proportional de-vig |
| nový `vb/fair_probability.py` | proportional/power/odds-ratio/consensus, model version |
| `vb/matching.py` | global assignment, orientation a calibrated abstention |
| nový `vb/market_mapping.py` | selection/line remap pro same/swapped orientation |
| `vb/settlement.py` | exhaustive selection validation, Decimal/line validation, evidence version |
| nový `vb/execution.py` | decision verification, paper/real execution receipt |
| nový `vb/exposure.py` | event/site/market caps a idempotency |
| nový `vb/closing.py` | closing snapshot a CLV |
| `vb/evaluation.py` | executed-bet cohort, clusters, uncertainty, drawdown, no B-from-A |
| `dashboard_template.html` | render jednoho evaluation JSON; confirmatory/exploratory oddělení |
| `scripts/scheduled_run.py` | source results, exit codes, žádná pipeline po failed source |
| `scripts/merge_databases.py` | po centralizaci jen idempotentní import přes UUID/manifests |
| `scripts/build_dashboard.py` | evaluation artifact s SHA/config/data cutoff |
| `.github/workflows/capture.yml` | krátkodobé permissions/artifacts; po VPS jen CI/deploy |
| `requirements.txt` | nahradit `pyproject.toml` + lockfile |
| `tests/` | restart, stale/skew, merge/prune, Method B, delayed entry, workflow fault tests |

## 20. Audit současného dashboardu

### Co je konzistentní

- hlavní Method-A počty, profit a ROI odpovídají vloženým JSON datům;
- bucket/site/market rozpady se sčítají;
- přepnutí flat/Kelly používá popsané vzorce;
- vstupní threshold mění first eligible snapshot v uložené A trajektorii;
- stránka se načetla bez browser console error.

### Co je zavádějící

- výchozí convergence filtr vypadá jako strategie, ale používá budoucí délku;
- „Method B agrees“ není samostatný Method-B entry replay;
- `Kelly ROI` vypadá jako růst bankrollu, ale denominator je součet malých
  simulovaných stake;
- legs nejsou vedle headline vždy doplněné počtem unikátních eventů;
- filtry usnadňují in-sample optimalizaci bez multiple-testing upozornění;
- graf rekonstruované pre-entry historie nemá provenance původního
  rozhodnutí;
- source health a data staleness nejsou viditelné;
- legacy data se tváří jako výkon aktuální verze.

### Doporučené headline pořadí

1. data health a cutoff;
2. strategy version a experiment status;
3. signals → decisions → verified → accepted → settled funnel;
4. unique events i bets;
5. flat net ROI + event-cluster interval;
6. CLV + interval;
7. slippage/rejection;
8. drawdown/exposure;
9. exploratory breakdowns;
10. Kelly až jako oddělená sizing simulace.

## 21. Odpovědi na otázky zadání

### Jak je projekt vytvořen po programové stránce?

Jde o čitelný Python POC se SQLite persistence, scraper moduly, fuzzy
matchingem, opportunity state machine, settlementem, evaluátorem a statickým
JavaScript dashboardem. Výpočtové jádro je malé a dobře testovatelné.
Nejslabší je spojení mezi procesy: run orchestrace, trvalá identita,
časová synchronizace, merge, provenance a reporting cohort.

### Jak funguje po funkční stránce?

Porovnává poslední známé ceny Pinnacle se Swisslos/Loro, otevře Method-A
opportunity při 3% cenovém rozdílu a později ji spáruje se score. Funkčně
však „poslední známý“ neznamená „současně dostupný“ a opportunity neznamená
provedenou sázku. Výsledkem je detektor historických cenových rozdílů, ne
zatím auditovatelný betting engine.

### Funguje algoritmus?

Technicky generuje signály a umí je settlementovat. Ekonomická část není
prokázaná. Method A není fair-EV model, Method B má chybný replay, data jsou
stale/nesynchronizovatelná a sample je příliš malý.

### Obsahuje faktické chyby?

Ano. Nejdůležitější reprodukované chyby:

- restart/recross může přepsat starou opportunity historii;
- event-start close používá stale captured time a přidá falešný snapshot;
- stale kurzy bez limitu otevřou opportunity;
- Method B opomíjí pozdější vlastní crossing;
- convergence filtr používá budoucí informaci;
- pruning může zachovat starší timestamp;
- workflow ztratilo celý úspěšný handicap capture.

### Jsou současná čísla správná?

- Method A 3 % / 0 min: aritmeticky ano, +5,65 u / +9,576 %.
- Výchozí Method A 3 % / 5 min: aritmeticky ano, −0,25 u / −0,595 %.
- Současná Method B: aritmeticky ano pro chybnou podmnožinu, funkčně ne.
- Settlement P&L: aritmeticky ano.
- Statistický závěr „kladné ROI“: ne.
- Připsání výkonu aktuálnímu kódu: ne.

### Lze algoritmus zlepšit pro vyšší ROI?

Potenciálně ano, hlavně:

1. odstranit stale/skew false positives;
2. měřit accepted odds a slippage;
3. nahradit vigged Method A kalibrovaným sharp consensusem;
4. testovat power/odds-ratio de-vig out-of-sample;
5. používat net-EV error buffer;
6. zavést skutečný online persistence vstup;
7. omezit eventovou korelaci a opakované bets;
8. měřit CLV;
9. zvolit segmenty až v time-split validaci.

Tyto změny nejprve zlepší platnost měření. Zda zvýší ROI, musí potvrdit nový
zmrazený experiment.

## 22. Konečný závěr

Projekt je nadprůměrně zdokumentovaný rychlý POC a jeho autor otevřeně
zaznamenal několik vlastních chyb. Core edge/settlement aritmetika není hlavní
problém.

Hlavní problém je experimentální design:

- rozhodnutí nevychází ze synchronních a prokazatelně fresh quotes;
- trvalá identita a historie nejsou bezpečné přes restart/merge;
- dashboard simuluje pravidla s budoucí informací;
- Method B není samostatně přehrána;
- výkonová data vznikla během měnícího se kódu;
- neexistuje execution ani provenance vrstva;
- sample nedává úzký interval ROI.

Současné `+9,576 %` bez convergence filtru ani `−0,595 %` ve výchozím view
není spolehlivý odhad budoucího ROI. Method-B `+47 %` je zvlášť nevhodné
headline číslo: vychází z pěti legs a chybné entry definice.

Další práce má začít opravou datového toku, ne optimalizací thresholdů.
Po v2 cutoveru musí vzniknout nový paper dataset bez zpětného přepisování.
Teprve jeho out-of-sample net ROI, CLV, execution rate a event-cluster
interval mohou rozhodnout, zda má strategie praktickou hodnotu.

---

## Příloha A — Reprodukční dotazy

### A.1 Hlavička opportunity vs první dochovaný snapshot

```sql
WITH first_snapshot AS (
  SELECT
    opportunity_instance_id,
    MIN(captured_at) AS first_snapshot_at
  FROM opportunity_snapshot
  GROUP BY opportunity_instance_id
)
SELECT
  o.opportunity_instance_id,
  o.market_key,
  o.first_cross_at,
  f.first_snapshot_at
FROM opportunity o
JOIN first_snapshot f
  ON f.opportunity_instance_id = o.opportunity_instance_id
WHERE o.first_cross_at <> f.first_snapshot_at
ORDER BY o.first_cross_at;
```

Výsledek: 16 řádků.

### A.2 Nejvyšší ID není nejnovější timestamp

```sql
WITH per_key AS (
  SELECT
    site,
    event_id,
    market_type,
    line,
    MAX(id) AS max_id,
    MAX(captured_at) AS max_captured_at
  FROM raw_market_snapshot
  GROUP BY site, event_id, market_type, line
)
SELECT COUNT(*)
FROM per_key p
JOIN raw_market_snapshot r ON r.id = p.max_id
WHERE r.captured_at < p.max_captured_at;
```

Pro NULL-safe produkční verzi je nutné nahradit běžné porovnání `line`
explicitním `IS`/normalizovaným key. Výsledek auditu: 3 292 market keys.

### A.3 První Method-B crossing v uložené trajektorii

```sql
WITH b_cross AS (
  SELECT
    opportunity_instance_id,
    MIN(captured_at) AS b_first_cross_at
  FROM opportunity_snapshot
  WHERE edge_b >= 0.03
  GROUP BY opportunity_instance_id
)
SELECT COUNT(*)
FROM b_cross;
```

Výsledek: 24 trajectories; 12 mají dostupný settlement.

### A.4 Event-level denominator

Počet legs se nesmí vydávat za počet nezávislých zápasů. Canonical event zde
není uložen, proto audit použil normalizovanou settled event identitu a
všechny legs stejného zápasu resamploval jako jeden cluster.

## Příloha B — Klíčová místa kódu

Odkazy jsou připnuté na auditovaný commit:

- [workflow schedules a concurrency](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/.github/workflows/capture.yml#L8-L41);
- [workflow backup a deploy permissions](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/.github/workflows/capture.yml#L109-L151);
- [capture pořadí, exception handling a pipeline](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/scripts/scheduled_run.py#L174-L208);
- [latest snapshot bez age limitu](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/vb/storage.py#L173-L210);
- [pipeline latest market a event-start](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/vb/pipeline.py#L216-L260);
- [opportunity instance sequence a lifecycle](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/vb/opportunity.py#L158-L285);
- [opportunity persistence](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/vb/storage.py#L230-L288);
- [raw pruning](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/vb/storage.py#L546-L580);
- [edge a proportional de-vig](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/vb/edge.py#L1-L62);
- [greedy matching](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/vb/matching.py#L82-L152);
- [Method-B evaluace](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/vb/evaluation.py#L122-L255);
- [merge opportunity a reconciliation](https://github.com/Dexter696/value-betting-poc/blob/e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1/scripts/merge_databases.py#L228-L350).

## Příloha C — Externí zdroje

Metodika a provoz:

- [GitHub Actions concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency);
- [GitHub scheduled workflow troubleshooting](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows);
- [Clarke, Kovalchik, Ingram: Adjusting Bookmaker's Odds to Allow for Overround](https://doi.org/10.11648/j.ajss.20170506.12);
- [Kelly: A New Interpretation of Information Rate](https://doi.org/10.1002/j.1538-7305.1956.tb03809.x).

Vzorek ručně kontrolovaných výsledků:

- [UEFA Europa League qualifying results](https://www.uefa.com/uefaeuropaleague/news/02a6-20e5db0029dd-8241a8d00925-1000--europa-league-qualifying-fixtures-dates-how-it-works/);
- [FC St. Gallen – Benfica 2:1](https://www.fcsg.ch/pages/news/grandioser-sieg-gegen-benfica);
- [FC Twente results](https://fctwente.nl/teams/eerste-selectie/uitslagen);
- [Galatasaray football news/results](https://www.galatasaray.org/haberler/futbol/43?q=nya);
- [Swiss Football League match center](https://matchcenter-sfl.football.ch/Default.aspx?a=msp&ln=11016&lng=2&ls=25693&oid=2&s=2027&sg=70074);
- [Qarabağ official result](https://qarabagh.com/az/news/ilk-oyunda-qolsuz-beraberlik/13713).

## Příloha D — Auditní omezení

- Audit neměl přístup k bookmaker účtům ani reálným bet receipts.
- Neproběhlo nové scraping zatížení proti bookmakerům; audit použil uložená
  data a veřejné workflow logy.
- Raw historie po 24hodinovém pruningu chybí.
- Manual settlement evidence není v DB.
- Bootstrap interval popisuje tento malý vzorek; sám neopravuje selection
  bias, stale data ani měnící se kód.
- Navržené thresholdy freshness a minimální sample jsou výchozí protokol,
  ne empiricky potvrzené optimum.
- Doporučení k ROI jsou testovatelné hypotézy, ne příslib výnosu.
