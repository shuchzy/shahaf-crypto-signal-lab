# Shahaf Crypto Signal Lab

מערכת מקומית מבוססת דפדפן לסריקת זוגות הקריפטו בעלי נפח המסחר הגבוה ביותר מול USDT.

Local browser-based cryptocurrency market scanner using transparent multi-timeframe
technical heuristics and a small online learning model.

## הפעלה

לחיצה כפולה על `start.bat`, או:

```powershell
python app.py
```

לאחר ההפעלה נפתח הדפדפן בכתובת `http://127.0.0.1:8765`.

אפשרויות שימושיות:

```powershell
python app.py --top 20 --interval 15
python app.py --no-browser
```

## מה המערכת מנתחת

- מגמה באמצעות EMA 20/50/200
- מבנה שוק HH/HL ו-LH/LL
- Liquidity sweeps
- Fair Value Gaps
- Order Blocks היוריסטיים
- שלבי Wyckoff משוערים
- RSI, ATR, נפח וחריגות נפח
- הסכמה בין 15m, 1h, 4h ו-1d
- מודל הסתברותי מקומי שמתעדכן מתוצאות איתותים לאחר 4 שעות

הנתונים נשמרים מקומית ב-`data/signals.db`. אין צורך במפתח API והמערכת אינה מבצעת עסקאות.

## Requirements

- Python 3.11 or newer
- Internet access for public Binance market data
- No API key is required

## מגבלות חשובות

המונחים ICT, Wyckoff, FVG ו-Order Blocks אינם בעלי הגדרה מתמטית יחידה. המימוש משתמש
בחוקים עקביים ושקופים, אך אינו יכול להבטיח שהפרשנות זהה לזו של סוחר מסוים. המודל לומד
רק מתוצאות שנמדדו ונשמרו מקומית, ולכן נדרשת תקופת איסוף נתונים לפני שיש משמעות
סטטיסטית לאחוזי ההצלחה.

## Disclaimer

This project is for research and educational use only. It is not financial advice,
does not guarantee profitable signals, and does not place trades.
