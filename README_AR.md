# ارفع هذه الملفات فقط — بوت Discord Python

هذه نسخة تشغيل مسطّحة: بعد فك الضغط في **جذر الخادم** يجب أن ترى `bot.py` و`requirements.txt` مباشرة، لا داخل مجلد فرعي. ملفات الوحدات المساعدة `.py` كلها مطلوبة؛ ارفعها كلها ولا تنتقِ بعضها. الملف الذي يشغّل البوت هو `bot.py`.

## إعداد Prime Host

السجل الذي أرسلته لا يقول إن ملفات Python ناقصة؛ أمر البدء ما زال يحاول تشغيل `ts-node`، وهو مشغّل Node غير موجود. من صفحة **Startup** استبدل أمر Node كاملًا بهذا:

```sh
python3 -u bot.py
```

لا تستخدم `MAIN_FILE` أو `ts-node` أو `package.json` لتشغيل هذا البوت. اختر Docker Image الخاص بـPython 3.12؛ تم تحديث المشروع والحزمة لاستهداف Python 3.12 أو أحدث.

إذا لم يثبت الـEgg الاعتماديات تلقائيًا من `requirements.txt`، ثبّتها في خطوة التثبيت الخاصة بالـEgg:

```sh
python3 -m pip install -r requirements.txt
```

حسب توجيه دعم Prime Host، احفظ الإعدادات في ملف اسمه `.env` داخل جذر الخادم، بجانب `bot.py`. يحمّل البوت هذا الملف عند التشغيل؛ ومتغيرات البيئة الموجودة أصلًا في النظام لها الأولوية:

```dotenv
DISCORD_BOT_ENV=production
DISCORD_TOKEN=PASTE_PRODUCTION_BOT_TOKEN_HERE
GROQ_API_KEY=PASTE_GROQ_KEY_HERE
DATABASE_URL=PASTE_POSTGRESQL_CONNECTION_URL_HERE
```

استبدل كل قيمة تجريبية بالقيمة الحقيقية من دون ترك الأقواس أو إرسالها في المحادثة. أنشئ الملف على Prime Host يدويًا؛ لا تضعه داخل الحزمة ولا ترفعه إلى Git. اختياري: `GEMINI_API_KEY` لتحليل الصور، `OPENROUTER_API_KEY` و`GROQ_API_KEY_2` كمزودي احتياط، و`PORT` لمنفذ فحص الصحة إذا طلبه الـEgg.

## لا ترفع هذه الأشياء

- لا ترفع مجلدات `node_modules` أو `node-starter`، ولا `package.json` أو ملفات Replit.
- لا ترفع مجلد `data/` القديم أو ملفات مفاتيح الدخول داخل هذا الأرشيف. خذ نسخة احتياطية من بيانات الخادم القديمة قبل إعادة التثبيت؛ الإعدادات المحلية وبيانات اللعبة ليست ضمن الحزمة.
- يلزم PostgreSQL في `DATABASE_URL`؛ MariaDB لا يعمل بدلًا منه. بيانات اللعبة الموجودة في قاعدة قديمة لا تنتقل مع ملفات الكود.

يلزم تفعيل Message Content Intent في Discord. الموسيقى تحتاج أيضًا FFmpeg وOpus على نظام الاستضافة.