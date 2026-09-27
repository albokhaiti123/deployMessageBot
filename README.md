# 🤖 DeployMessageBot — بوت بث المحتوى الحصري

بوت تليجرام متكامل لبث المحتوى المدفوع والمجاني مع نظام دعوات وذاتية التدمير.

---

## ✨ المميزات

| الميزة | الوصف |
|--------|-------|
| 🔗 نظام الدعوات | كل مستخدم يمتلك رابط دعوة خاص به |
| 📨 بث المحتوى | نص / صور / فيديو / صوت / مستندات |
| 💰 الدفع بالنجوم | محتوى مدفوع عبر Telegram Stars ⭐ |
| 🔒 حماية المحتوى | منع التوجيه والنسخ (`protect_content`) |
| 💣 ذاتية التدمير | حذف فوري أو بعد فترة زمنية محددة |
| ⏰ الجدولة | إرسال فوري، مجدول، أو متكرر |
| 🛠️ لوحة تحكم | واجهة كاملة داخل البوت للأدمن |
| 📊 الإحصائيات | عدد المشتركين، الإيرادات، الرسائل المرسلة |

---

## 🚀 التثبيت والتشغيل

### 1. استنساخ المستودع
```bash
git clone https://github.com/albokhaiti123/deployMessageBot.git
cd deployMessageBot
```

### 2. إنشاء البيئة الافتراضية
```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate
```

### 3. تثبيت المتطلبات
```bash
pip install -r requirements.txt
```

### 4. إعداد المتغيرات البيئية
```bash
cp .env.example .env
# عدّل ملف .env وأدخل القيم المطلوبة
```

### 5. تشغيل البوت

**تشغيل مباشر:**
```bash
python main.py
```

**تشغيل عبر PM2 (موصى به):**
```bash
pm2 start ecosystem.config.js
pm2 save
pm2 startup
```

---

## ⚙️ إعداد `.env`

```env
BOT_TOKEN=1234567890:AAXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
ADMIN_IDS=123456789,987654321
BOT_USERNAME=YourBotUsername
DATABASE_URL=sqlite:///bot_database.db
INVITE_ONLY=true
SCHEDULER_TIMEZONE=Asia/Riyadh
LOG_LEVEL=INFO
```

---

## 🛠️ أوامر الأدمن

| الأمر | الوظيفة |
|-------|---------|
| `/admin` | فتح لوحة التحكم |
| `/cancel` | إلغاء العملية الحالية |

### خطوات إنشاء رسالة (ويزارد):
1. العنوان الداخلي
2. نوع الوسائط (نص/صورة/فيديو/صوت/ملف)
3. رفع المحتوى أو كتابة النص
4. مجاني أم مدفوع؟
5. سعر النجوم (إن كان مدفوعاً)
6. حماية المحتوى
7. وضع ذاتية التدمير
8. جدولة الإرسال
9. تأكيد وإرسال

---

## 🗂️ هيكل المشروع

```
deployMessageBot/
├── main.py                     # نقطة الدخول
├── config.py                   # الإعدادات
├── ecosystem.config.js         # إعدادات PM2
├── requirements.txt
├── .env.example
├── database/
│   ├── models.py               # نماذج قاعدة البيانات
│   └── db.py                   # عمليات CRUD
└── bot/
    ├── handlers/
    │   ├── user.py             # أوامر المستخدمين
    │   ├── admin.py            # لوحة التحكم
    │   └── payment.py          # نظام الدفع
    └── services/
        ├── broadcast.py        # خدمة البث
        ├── scheduler.py        # جدولة المهام
        └── referral.py         # روابط الدعوة
```

---

## ⚠️ ملاحظات مهمة

> **لقطة الشاشة:** تليجرام لا يوفر API لمنع لقطة الشاشة في البوتات. `protect_content=True` يمنع التوجيه والنسخ فقط.

> **النجوم (Stars):** تأكد من تفعيل المدفوعات عبر @BotFather → Bot Settings → Payments.

> **قاعدة البيانات:** يستخدم SQLite افتراضياً. للإنتاج، يُنصح بـ PostgreSQL.

---

## 📄 الترخيص

MIT License — استخدم بحرية مع الحفاظ على الإشارة للمصدر.
