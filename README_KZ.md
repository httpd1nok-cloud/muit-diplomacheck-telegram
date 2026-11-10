# MUIT DiplomaCheck · Telegram Cloud (орысша)

**Мақсат:** Telegram боты MacBook/Terminal жабылғанда да жұмыс істеуі үшін оны тұрақты серверге көшіру.
Бұл нұсқа Telegram құжатын **тікелей Python тексеру модулімен** талдайды: веб-сайт та, FastAPI да, WhatsApp та қажет емес.

## 1. Бұл қандай нұсқа?

- Сақталған МУИТ методичкасы: `reference/muit_methodology_2020.pdf` (18.11.2020).
- `PDF` және `DOCX` қабылдайды (20 МБ-қа дейін).
- Орысша `/start`, `/help`, `/criteria` және тексеру нәтижесі.
- Толық JSON есепті жібереді; файлдар уақытша сақталып, өшіріледі.
- Бір қолданушыға сағатына 5 құжат шектеуі; алғашқы сынаққа `ALLOWED_TELEGRAM_USER_IDS` арқылы рұқсат етілген адамдар тізімін қоюға болады.
- Бұл ресми университет бағасы да, плагиат тексеруі де емес.
- Docker-дың жеңіл нұсқасында LibreOffice жоқ: PDF беттерін есептейді, бірақ DOCX беттің нақты санын әрдайым анықтай алмайды. Бет саны маңызды болса, PDF тапсыру ұсынылады.

## 2. GitHub-қа жеке репозиторий ретінде салу

1. https://github.com кір; аккаунтың жоқ болса, тіркел.
2. **New repository** → мысалы `muit-diplomacheck-telegram` → **Private** → Create repository.
3. Токен сақталмаған осы буманың файлдарын репозиторийге жүкте. `.env` файлды **ЕШҚАШАН** жүктеме; архивте оның өзі жоқ, тек `.env.example` бар.
4. Қалта құрылымы сақталғанын тексер: репозиторийдің түбірінде `Dockerfile`, `requirements.txt`, `app/`, `config/`, `reference/` болуы тиіс.

Егер браузердегі файл жүктеу ішкі қалталарды дұрыс сақтамаса, Terminal арқылы жүктеу жолы:

```bash
cd ~/Downloads/MUIT_DiplomaCheck_Telegram_Cloud
# Алдымен GitHub-та БОС private repository аш. Төмендегі URL-ді өз репозиторийіңмен алмастыр.
git init
git add .
git commit -m "Deploy Telegram DiplomaCheck worker"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/muit-diplomacheck-telegram.git
git push -u origin main
```

## 3. Railway серверіне көшіру

Бұл — Railway сияқты контейнерлерді тоқтатпай ұстай алатын хостингке арналған жоба. Оның **бағасы, free/trial шектеулері және төлем талаптары өзгеруі мүмкін**; аккаунттың Billing бөлімінен қара. 24/7 жұмысты тек белсенді және ұйқыға кетпейтін қызмет қамтамасыз етеді.

1. https://railway.com/ кіріп, GitHub арқылы кір.
2. New Project → Deploy from GitHub repository → `muit-diplomacheck-telegram` репозиторийін таңда.
3. Қызмет `Dockerfile` негізінде жиналуы керек. Ондағы команда: `python -m app.bot_cloud`.
4. Қызметтің **Variables** бөлімін ашып, жаңа құпия айнымалы енгіз:
   - `TELEGRAM_BOT_TOKEN` = BotFather берген өзіңнің токенің.
   - `ALLOWED_TELEGRAM_USER_IDS` = алғашқы тестте өз Telegram user ID-ің (міндетті емес; бос болса, барлығына ашық).
5. Deploy жаса. Logs-тен `Telegram worker starting (polling; no website required).` және Telegram API сұраулары көрінсе, бот іске қосылған.
6. Railway қызметінің **ұйқыға кетпейтін** режимде екенін және **бір ғана replica** жұмыс істейтінін тексер. Public domain, webhook немесе PORT керек емес.
7. Telegram-да **бұрынғы дәл сол ботқа** `/start`, `/criteria`, PDF/DOCX жібер.
8. MacBook-тағы ескі ботты **Control + C** арқылы тоқтат; бір токенмен екі polling бот қатар тұрса, Telegram 409 Conflict қатесін береді.
9. MacBook-ты жауып, телефоннан Telegram арқылы тексер: сервер жұмыс істеп тұрса, жауап береді.

**Маңызды:** ескі ботыңды сервердегі жаңа бот дайын болғанға дейін өшірмеуге болады, бірақ бір токенмен екеуін бір мезгілде қосып тексерме. Алдымен ескі ботты тоқтатып, содан кейін бұлттағы ботты іске қос.

## 4. Қателер және шешімдері

- `TELEGRAM_BOT_TOKEN не задан` → Railway Variables ішіне токен енгізілмеген.
- `Unauthorized` → токен қате немесе BotFather арқылы ауыстырылған.
- `Conflict: terminated by other getUpdates request` → MacBook/басқа серверде осы бот әлі қосулы; тек біреуін қалдыр.
- `Бот жауап бермейді` → Railway deployment, logs, billing/status және worker running күйін тексер; қызмет ұйқыға кетуі мүмкін.
- `.docx` бет санын тексермейді → бұл жеңіл Docker нұсқасында LibreOffice жоқ. Құжатты PDF-ке түрлендіріп жібер.
- `PDF` өте үлкен → 20 МБ және 400 бет лимиті бар.

## 5. Қауіпсіздік пен құпиялық

- Telegram-ға жіберілген диплом алдымен Telegram инфрақұрылымы арқылы өтеді, одан кейін серверде өңделеді.
- Жүктелген файл тек өңдеу кезінде уақытша сақталады; JSON есеп Telegram чатқа жіберіледі.
- API кілтсіз жұмыс істейді; сыртқы AI-ға мәтін жіберілмейді.
- `.env`, BotFather токені және пайдаланушылардың жұмыстары GitHub репозиторийінде сақталмауы керек.
- Тексеруді бастапқыда өз ID-іңмен шектеу ұсынылады; кейін көпшілікке рұқсат беруге болады.
- Ботты жалпыға ашпас бұрын университеттің деректерді өңдеу/рұқсат беру талаптарын тексер.

## 6. Жергілікті тест (міндетті емес)

```bash
cd ~/Downloads/MUIT_DiplomaCheck_Telegram_Cloud
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
nano .env   # Токенді тек жергілікті .env файлына енгіз
python -m app.bot_cloud
```

**Мақсаты:** жергілікті кодты тексеру; 24/7 үшін Railway қызметі керек. Орналастыру аяқталмайынша мен сенің ботың серверде жұмыс істейді деп айта алмаймын.
