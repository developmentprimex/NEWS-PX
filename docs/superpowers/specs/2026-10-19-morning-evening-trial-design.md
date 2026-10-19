# Prova njëditore e kontrollit të mëngjesit dhe raportit të mbrëmjes

## Synimi dhe kufijtë

Të provohet për një ditë lokale rrjedha manuale në mëngjes dhe automatike në 21:00, duke matur përdorimin real të Bright Data përpara vendimit për përdorim të vazhdueshëm. Kjo është një ndryshim i koordinimit të fetching dhe emailit; nuk është ende aktivizim në live.

Kërkesat e pranuara nga përdoruesi:

- “Check for updates” nis kontroll real të burimeve aktive, përfshirë LinkedIn posts dhe reposts.
- Në 21:00 kryhet kontrolli i mbrëmjes dhe pastaj dërgohet emaili.
- Emaili përmban vetëm aktivitetet pas kontrollit të suksesshëm të mëngjesit. Rezultatet e mëngjesit mbeten në feed.
- Nëse mungon kontrolli i mëngjesit, përdoret pika e fundit e suksesshme dhe rezultatet e atij intervali dërgohen me email.
- Fillimisht matet një ditë; më pas vendoset si vazhdohet.
- Mbrojtjet ekzistuese për dështimet dhe arritjen e limitit 6 ruhen. Riprojektimi i rikuperimit dhe i mbulimit mbi limitin mbetet jashtë kësaj prove.

Kufijtë mbeten 6 rekorde për kërkesë LinkedIn, 130 rekorde të rezervuara në ditë UTC, 4000 buxhet operacional mujor dhe 1000 rezervë. Prova nuk ndryshon planin e provider-it dhe nuk autorizon tejkalimin e këtyre kufijve.

## Ora dhe përmbajtja e emailit

Orari është `Europe/Budapest`, i njëjtë me orën lokale të Kosovës në datat e provës. Ora 21:00 kthehet në UTC me rregullat e zonës, përfshirë ndryshimin verë/dimër.

Për një klikim në 08:00, pika e prerjes së kontrollit është ora kur serveri e pranon kërkesën. Ajo ruhet veçmas nga koha kur përfundon shkarkimi. Kështu një kontroll i gjatë nuk krijon boshllëk ndërmjet dy intervaleve.

| Veprimi | Intervali dhe rezultati |
| --- | --- |
| Kontrolli manual në 08:00 | Merr përditësimet deri në pikën 08:00; i ruan në feed. Pika e mbulimit avancohet vetëm për burimet që përfunduan me sukses. |
| Kontrolli automatik në 21:00 | Merr intervalin e pambuluar deri në pikën 21:00. Në rastin normal emaili përzgjedh aktivitetet në `(08:00, 21:00]`. |
| Fetching përfundon në 21:02 | Emaili përgatitet pas përfundimit; mund të përfshijë aktivitet të publikuar deri në 21:00 edhe nëse u ruajt në 21:02. |
| Nuk u bë kontroll manual | Kufiri i poshtëm vjen nga pika e fundit e suksesshme për burimin; emaili e shënon intervalin përkatës. |

Nëse kontrolli i mëngjesit përfundon pjesërisht, vetëm burimet me sukses përdorin pikën e mëngjesit. Të tjerat ruajnë pikën e fundit të provuar dhe statusin e pasigurisë; klikimi nuk përbën sukses. Kjo ruan mbrojtjen ekzistuese pa shtuar mekanizëm të ri rikuperimi.

Filtri i emailit përdor kohën e aktivitetit, jo vetëm `created_at` në bazën e aplikacionit. Një postim i vjetër i shkarkuar në mbrëmje nuk bëhet automatikisht aktivitet “pas mëngjesit”. Për reposts kërkohet ora e verifikueshme e repostimit; ora e postimit origjinal nuk përdoret si zëvendësim i saj. Aktivitetet me kohë të panjohur mund të ruhen në feed, por nuk paraqiten si aktivitet i konfirmuar brenda intervalit në email. Raporti evidenton pasigurinë.

Përzgjedhja e burimeve për email ruan preferencat ekzistuese **Email on** dhe marrësin privat të konfiguruar. Kontrolli i feed-it përfshin burimet aktive të mbështetura; ky ndryshim nuk aktivizon në heshtje abonime të reja me email.

## Koordinimi dhe ruajtja e gjendjes

Zgjidhet një cikël i ruajtur në PostgreSQL. Lidhja vetëm me task-e në memorie do të humbiste pikën e mëngjesit gjatë restartit. Dy procese të pavarura për fetching dhe email do të lejonin raportin përpara përfundimit të snapshot-eve. Cikli i ruajtur lidh intervalin, ekzekutimet dhe dërgesën në të njëjtin historik të rishikueshëm.

Cikli ruan datën lokale të provës, fazën `manual` ose `evening`, pikën e prerjes, listën e burimeve dhe gjendjen e përpunimit. Për çdo burim lidhet ekzekutimi Bright Data dhe pika e fundit e mbulimit të suksesshëm. Rregullat janë:

- Një klikim i përsëritur kthen gjendjen e kontrollit ekzistues; nuk blen një scraping të dytë të së njëjtës fazë.
- Skanimi i scheduler-it mund të vazhdojë polling të snapshot-it të njohur. Nuk nis kontrollin e mëparshëm `scheduled-day` paralelisht për burimet e provës.
- Rezervimi i krediteve kryhet dhe ruhet përpara çdo POST, nën kyçet ekzistuese të llogarisë dhe identitetit të burimit.
- Një snapshot i pjesshëm, gabim ose rezultat i pasigurt nuk avancon mbulimin. Kufiri i raportit dhe statusi i mbulimit ruhen veçmas.
- Restart-i ringarkon gjendjen e ciklit, jo një koleksion të ri nga fillimi.
- Historiku i vjetër nuk shënohet i mbuluar nga kjo provë. Migrimi i pikave të vjetra përdor vetëm evidencën ekzistuese të suksesit; boshllëqet ose pasiguria bëhen të dukshme dhe nuk shkaktojnë backfill të pakufizuar.

Burimi pa pikë të provuar mbetet me mbulim të paverifikuar dhe i nënshtrohet kufijve ekzistues të nisjes. Ora e aktivizimit të provës nuk përdoret si sukses i sajuar për historikun.

Butoni dhe endpoint-et ekzistuese të progresit përdorin këtë cikël. “Refresh feed” mbetet lexim i rezultateve të ruajtura. Kontrolli manual me konsum kreditesh ruan autorizimin ekzistues të administratorit.

## LinkedIn dhe saktësia e intervalit

Kontrata e dokumentuar e filtrimit përdor data. Aplikacioni kërkon intervalin e datave që përmban intervalin kohor dhe pastaj përzgjedh aktivitetet sipas orës. Dublikatat rifreskojnë të dhënat ekzistuese pa krijuar një kartë të dytë.

Provider-i mund të rikthejë në mbrëmje rekorde të marra në mëngjes; filtrimi lokal nuk e anulon konsumin e tyre. Matja duhet ta bëjë këtë kosto të dukshme. Prova nuk premton mbulim të plotë kur arrihet limiti 6 ose mungon ora e aktivitetit.

Referencë: [LinkedIn API Scrapers](https://docs.brightdata.com/cn/api-reference/web-scraper-api/social-media-apis/linkedin). Mbështetja faktike e kufijve sipas orës dhe e datës së repostimit verifikohet me të dhëna reale brenda kufijve të provës; nuk supozohet nga pranimi i një string-u ISO.

## Dërgimi në mbrëmje

Në 21:00 ngrihet faza e mbrëmjes. Emaili pret që burimet e fazës të arrijnë një gjendje përfundimtare, deri në 30 minuta nga pika 21:00. Dërgimi normal synohet pas përfundimit dhe mund të ndodhë disa minuta pas 21:00. Pas afatit, raporti përdor rezultatet e disponueshme dhe evidenton burimet ende në pritje. Polling i snapshot-eve ekzistuese mund të vazhdojë; nuk dërgohet raport i dytë automatik për të njëjtën ditë.

Burimet e papërfunduara ose të bllokuara shënohen si të tilla në raport; nuk paraqiten si burime të kontrolluara me zero rezultate. Dërgimi nuk e avancon pikën e mbulimit të një burimi të dështuar. Ruhet një identitet i vetëm i raportit për ditën dhe marrësin. Një rezultat i pasigurt SMTP kërkon verifikim dhe nuk shkakton ridërgim të verbër.

Emaili i provës zëvendëson dërgesat automatike të tjera për të njëjtën ditë dhe marrës. Orari i vjetër 12:30/17:00 nuk dërgon përmbajtjen e mëngjesit. SMTP lejohet vetëm për rrjedhën e autorizuar të provës; guard-i aktual i serverit e çaktivizon emailin dhe duhet përshtatur në mënyrë të kufizuar. Aktivizimi i emailit bëhet vetëm pasi të jenë verifikuar konfigurimi privat, lista e burimeve dhe marrësi. Kredencialet nuk hyjnë në kod, dokument ose log publik.

## Matja e krediteve dhe kohëzgjatja e provës

Përpara aktivizimit ruhet baseline-i i përdorimit të llogarisë së lidhur. Lexime të tjera bëhen para dhe pas kontrollit manual, para dhe pas kontrollit të mbrëmjes dhe pas përditësimit të rollup-it të provider-it. Këto janë GET-e të përdorimit, jo scraping shtesë.

Raporti i kostos dallon:

- numrin e kërkesave të reja POST dhe ekzekutimet e lidhura;
- rekordet e kthyera, dublikatat dhe aktivitetet e reja të ruajtura;
- rezervimin konservativ të aplikacionit;
- ndryshimin e përdorimit që raporton Bright Data, muajin dhe kohën e evidencës;
- përdorimin tjetër të llogarisë që pengon atribuimin vetëm te prova.

Nëse rollup-i nuk pasqyron ende përfundimin e snapshot-eve, kostoja shënohet **provizore**. Nuk paraqitet si zero dhe nuk nxirret nga numri i kartave të reja. Monitori ekzistues orar i faturimit dhe rezervimet e vjetra ruhen.

Shembull rezervimi: 8 profile × 2 faza × 6 = 96 rekorde; 20 profile kërkojnë 240 dhe nuk lejohen nga kufiri ditor 130. Pranë aktivizimit rillogaritet kapaciteti mbi përdorimin e asaj dite dhe burimet reale. Prova nuk rrit limitet për të futur të gjitha burimet me çdo kusht.

Data e provës vendoset vetëm gjatë aktivizimit: dita lokale aktuale nëse aktivizimi bëhet para 21:00, përndryshe dita pasuese. Në mbylljen e asaj dite nuk krijohen faza të reja të provës. Punët e nisura, raporti i vetëm ende në pritje për atë cikël dhe mbledhja e vonuar e evidencës së përdorimit mund të përfundojnë edhe pas mesnatës. Më pas kthehet politika e mëparshme e kontrollit ditor; pikat e paprovuara nuk shënohen si ditë të mbuluara. Ajo politikë mund të rishikojë një datë të prekur pjesërisht nga prova, gjithmonë brenda buxhetit dhe duke ruajtur rezervimet. Dy kontrollet dhe emaili i ri nuk vazhdojnë automatikisht në ditët pasuese pa vendimin e përdoruesit.

## Verifikimi përpara aktivizimit

Testet duhet të provojnë rrjedhën nga butoni te ekzekutimi i ruajtur dhe nga faza 21:00 te raporti:

1. Aktiviteti para pikës së mëngjesit përjashtohet nga emaili; ai pas saj përfshihet vetëm një herë.
2. Rezultati i ruajtur pas 21:00, por me aktivitet brenda intervalit, nuk humbet nga raporti.
3. Pa kontroll manual përdoret pika e fundit e suksesshme; koha e klikimit ose krijimit të rreshtit nuk falsifikon suksesin.
4. Klikimet e përsëritura, dy worker-a dhe restart-i nuk krijojnë një POST ose email shtesë për të njëjtën fazë.
5. Zonat kohore dhe ndryshimi verë/dimër e vendosin ciklin në 21:00 lokale.
6. Limiti 6, dështimet e snapshot-it, POST-i i pasigurt dhe mungesa e buxhetit ruajnë mbrojtjet ekzistuese.
7. Leximet e përdorimit dallojnë kreditet e raportuara nga rezervimet dhe trajtojnë përditësimin me vonesë.
8. Pas skadimit nuk nisen faza të reja të provës. Një raport i ciklit të nisur mund të përfundojë pas mesnatës, pa krijuar raport për një ditë tjetër ose ridërgim.

Aktivizimi në live bëhet me backup të freskët, evidencë të versionit të testuar dhe verifikim të ruajtjes së burimeve, postimeve, ledger-it dhe preferencave të emailit. Ky projektim nuk pretendon se prova është zbatuar ose ekzekutuar.
