# בדיקות ושליטה בממשק AutoScript ב־Windows

מסמך זה הוא נוהל העבודה המלא לבדיקות פיזיות של יישומי AutoScript ב־Windows באמצעות `node_repl` וחבילת `@oai/sky`. הוא מיועד להרצה של ה־Interface, ה־Builder, ה־Runner וה־Analyzer, לרבות דיאלוגי קבצים של Windows.

## עקרונות מחייבים

1. קוראים תחילה את הוראות כלי ה־Computer Use ואת מסמכי `guidance`, `api` ו־`confirmations` של הגרסה המותקנת.
2. מפעילים את `@oai/sky` רק מתוך `node_repl`. אין לממש כלי UI חלופי ב־PowerShell, ואין לשלוט בטרמינל דרך ממשק Windows.
3. בוחרים חלון רק מתוך תוצאה אמיתית של `list_apps()` או `list_windows()`. לעולם אין להמציא מזהה חלון או לשחזר אובייקט חלון ידנית.
4. ממשיכים רק אם הסינון החזיר חלון יחיד בדיוק.
5. כל צילום, אינדקס accessibility וקואורדינטה תקפים רק לתצפית שיצרה אותם. לאחר פעולה מרעננים מצב לפני פעולה נוספת.
6. בכל תא פעולה מבצעים פעולת קלט אחת בלבד. אם התוצאה אינה ידועה, צופים מחדש לפני ניסיון נוסף.
7. אין לשמור, למחוק או להעלות מידע שאינו נתון בדיקה ייעודי. נתוני smoke מסומנים במפורש ונמחקים רק לאחר אישור או באמצעות cleanup מתוכנן.

## 1. אתחול

בכל kernel חדש:

```js
if (!globalThis.sky) {
  const { sky } = await import("@oai/sky");
  globalThis.sky = sky;
}
```

בגרסאות שבהן `sky.documentation()` קיים, קוראים לפני כל פעולה:

```js
await sky.documentation("guidance");
await sky.documentation("api");
await sky.documentation("confirmations");
```

אם המתודה אינה קיימת, קוראים את שלושת קובצי התיעוד מתוך חבילת ה־Computer Use המותקנת לפני שממשיכים. אין לדלג על כך.

## 2. איתור ובחירת חלון

```js
globalThis.apps = await sky.list_apps();
nodeRepl.write(JSON.stringify(apps, null, 2));
```

ליישום שכבר רץ אפשר להשתמש ב־`list_windows()`:

```js
globalThis.windows = await sky.list_windows();
globalThis.candidates = windows.filter((window) =>
  /AutoScriptInterface\.exe/i.test(String(window.app)) ||
  /^Touchpad Writing Experiment$/i.test(String(window.title || ""))
);
if (candidates.length !== 1) {
  nodeRepl.write(JSON.stringify(candidates, null, 2));
  throw new Error(`Expected one AutoScript window; found ${candidates.length}`);
}
globalThis.targetWindow = await sky.get_window({
  id: candidates[0].id,
  app: candidates[0].app,
});
await sky.activate_window({ window: targetWindow });
```

אם היישום אינו רץ:

```js
await sky.launch_app({ app: "C:\\absolute\\path\\Application.exe" });
```

לאחר מכן מרעננים את רשימת החלונות ומבצעים שוב סינון ליחיד.

## 3. קריאת מצב הממשק

### עץ accessibility

```js
globalThis.state = await sky.get_window_state({
  window: targetWindow,
  include_screenshot: false,
  include_text: true,
});
globalThis.targetWindow = state.window;
nodeRepl.write(String(
  state.accessibility?.tree || state.accessibility?.document_text || ""
));
```

משתמשים בעץ כדי לאמת כותרות, כפתורים, שדות, טבלאות, focus וטקסט מוצג. אינדקס רכיב נבחר רק אחרי קריאת העץ העדכני.

### צילום מסך

```js
globalThis.state = await sky.get_window_state({
  window: targetWindow,
  include_screenshot: true,
  include_text: false,
});
globalThis.targetWindow = state.window;
```

הצילום מוצג אוטומטית. אין לפענח או לשמור אותו רק לצורך צפייה.

### מעקף לגרסה שאינה מצליחה להציג צילום מתוך `node_repl`

אם `get_window_state(...include_screenshot: true)` נכשל ב־`node_repl exec context not found`, מותר להשתמש ב־transport של אותה חבילת Sky — לא בכלי UI חלופי:

```js
globalThis.rawState = await sky.transport.request("get_window_state", {
  window: targetWindow,
  include_screenshot: true,
  include_text: true,
});
nodeRepl.write(JSON.stringify(rawState));
```

ה־caller מציג את כתובות ה־data של `rawState.screenshots`. עדיין חלים כל כללי תוקף הצילום והקואורדינטות.

## 4. פעולות קלט

בכל הדוגמאות `observation` הוא המצב האחרון שנבדק. לאחר הפעולה מאפסים את המצב ומצלמים/קוראים מחדש.

### לחיצה על רכיב accessibility

```js
const observation = globalThis.state;
globalThis.state = null;
await sky.click({
  window: observation.window,
  element_index: 19,
});
```

אם UIA אינו מספק geometry (`coordinate input geometry is unavailable`), משתמשים בצילום וקואורדינטה.

### לחיצה בקואורדינטה

```js
const observation = globalThis.state;
const screenshotId = observation.screenshots[0].id;
globalThis.state = null;
await sky.click({
  window: observation.window,
  screenshotId,
  x: 420,
  y: 260,
  mouse_button: "left",
  click_count: 1,
});
```

במצב raw משתמשים באותה בקשה דרך `sky.transport.request("click", payload)`. הקואורדינטות הן יחסיות לחלון שאליו שייך הצילום. בדיאלוג modal משתמשים בצילום בעל `zIndex` הגבוה ביותר וב־`originX`/`originY` שהוחזרו.

### הקלדת טקסט

קודם מוודאים שה־focus נמצא בשדה הנכון. בתא הבא בלבד:

```js
const observation = globalThis.state;
if (!observation.accessibility?.focused_element) {
  throw new Error("Focused field was not observed");
}
globalThis.state = null;
await sky.type_text({ window: observation.window, text: "Test value" });
```

`type_text` שולח טקסט מילולי בלבד. אין לשלב בו תווי שליטה.

### החלפת ערך בשדה

```js
await sky.set_value({
  window: observation.window,
  element_index: 18,
  value: "Replacement value",
});
```

אם ValuePattern אינו זמין, לוחצים בשדה, שולחים `Control_L+a` בפעולה נפרדת, צופים מחדש, ואז מקלידים בפעולה נפרדת.

### מקשים וקיצורים

```js
await sky.press_key({ window: observation.window, key: "Return" });
await sky.press_key({ window: observation.window, key: "Escape" });
await sky.press_key({ window: observation.window, key: "Control_L+a" });
await sky.press_key({ window: observation.window, key: "Shift_L+Tab" });
```

משתמשים בשמות X11-style. אין להשתמש ב־Windows/Meta/Super key ואין לפתוח את Windows Run.

### גלילה

```js
await sky.scroll({
  window: observation.window,
  screenshotId: observation.screenshots[0].id,
  x: 600,
  y: 500,
  scrollX: 0,
  scrollY: 600,
});
```

ערך חיובי גולל מטה/ימינה; שלילי מעלה/שמאלה. לגלילה בתוך pane מסוים לוחצים בו קודם, מרעננים, ואז גוללים מתוך נקודה בתוכו.

### גרירה

```js
await sky.drag({
  window: observation.window,
  screenshotId: observation.screenshots[0].id,
  from_x: 300,
  from_y: 350,
  to_x: 300,
  to_y: 250,
});
```

משמש לשינוי סדר Blocks או לבדיקות קנבס/כתב יד. לאחר הגרירה מאמתים את הסדר מה־UI ומהנתונים, לא רק מהציור.

### פעולה משנית

```js
await sky.perform_secondary_action({
  window: observation.window,
  element_index: 22,
  action: "Expand",
});
```

משתמשים רק בפעולה שמופיעה במפורש בעץ ה־accessibility של אותה תצפית.

## 5. דיאלוגי קבצים

1. לוחצים על Upload/Download מתוך צילום עדכני.
2. קוראים `list_windows()`; אם ה־modal אינו מופיע כחלון עצמאי, מצלמים שוב את חלון האב.
3. כשמוחזרים כמה screenshots, בוחרים את זה בעל `zIndex` הגבוה ביותר.
4. ל־Upload Blocks בוחרים כמה קובצי ZIP באמצעות שדה `File name` עם שמות מלאים במרכאות, או באמצעות Ctrl/Shift selection.
5. לפני לחיצה על Open מאמתים שהקבצים הם נתוני בדיקה מהפרויקט. העלאת קבצים היא פעולה המחייבת pre-approval אם המשתמש לא ביקש אותה במפורש.
6. לאחר Open מאמתים ברשימת ה־Blocks את מספר הפריטים, שמותיהם והסדר. אין ללחוץ Save אלא אם יצירת/עדכון הניסוי היא חלק מפורש מהבדיקה.
7. דיאלוג Save נבדק באותה דרך, אך מבטלים אותו לאחר אימות אם אין צורך ביצירת קובץ.

## 6. תרחישי smoke של AutoScript

### Interface

- החלון עולה ומגיב.
- מוצגים ניסויי הענן ומספר Blocks בכל ניסוי.
- החיפוש מסנן מיידית.
- Refresh אינו יוצר כפילויות.
- תפריט Updates מציג Interface/Builder/Runner/Analyzer, גרסה מותקנת, גרסה אחרונה וסטטוס.
- New Experiment פותח תהליך Builder עצמאי.
- Edit, Run, Test Run ו־Analyze מנותבים לרכיב המתאים; אין להעביר token ב־argv.

### Builder

- גרסת Builder מוצגת.
- שם הניסוי ניתן לעריכה.
- Upload Blocks מקבל בחירה מרובה ומציג כל ZIP כ־Block.
- Add Block פותח את עורך ה־Block.
- סדר Blocks ניתן לגרירה.
- `same page as previous` נשמר ומופיע לאחר טעינה מחדש.
- Save הוא אטומי ומרענן את רשימת הענן.
- ZIP ישן ללא שדות חדשים מתקבל דרך שכבת התאימות.

### Runner

- ניסוי חד־Block ורב־Block נטענים.
- ניסוי ענן משתמש בסדר וב־page layout השמורים.
- ניסוי local/legacy מציג דיאלוג סדר Blocks וקליברציה.
- Run/Test Run יוצרים lifecycle תקין ותוצאות בעלות Experiment/Block/Session identity.
- בדיקות עט, calibration ואודיו אמיתי דורשות חומרה אנושית.

### Results/Analyzer

- מסך Exp Results מציג משתתפים, checkboxes, select-all וסטטוס מילולי/צבעוני.
- הורדת raw/CSV/trainable פועלת רק על המסומנים.
- Analyzer משחזר edit state לפי RunResult ID ו־SHA-256.
- autosave משתמש ב־ETag/CAS ובתור retry מקומי.
- finalization שומר state+CSV+trainable באופן אטומי ואת `analysis_completed`.
- JSON ישן/manual משוחזר ממאגר מקומי לפי hash גם לאחר rename.

## 7. אישורים ובטיחות

- מחיקה דרך UI: תמיד עוצרים ומבקשים אישור מיד לפני הלחיצה הסופית.
- התקנה או הרצה של תוכנה שהורדה זה עתה: תמיד מבקשים אישור. תוכנה שנבנתה מקומית או כבר מותקנת אינה דורשת אישור נוסף.
- העלאת קבצים: pre-approval מפורש מספיק; אחרת מבקשים אישור לפני Open/Upload.
- כניסה לחשבון או אישור permission: דורשים pre-approval או אישור בזמן הפעולה. אין לבצע אוטומציה של חלון authentication עצמו.
- אין לשנות הגדרות אבטחה/פרטיות, לעקוף אזהרות HTTPS, CAPTCHA או מנגנוני בטיחות.
- אין להעתיק token, סיסמה או מידע רגיש אל argv, log, צילום או מסמך בדיקה.

## 8. התאוששות מתקלות

- timeout בפעולה קלה: ממתינים שתי שניות ומנסים פעם אחת. אם נכשל שוב, מאפסים את kernel, מאתחלים ובוחרים חלון מחדש.
- stale handle: קוראים `get_window()` עם `id` ו־`app` שהוחזרו, או מריצים שוב `list_windows()`.
- חלון ממוזער: `activate_window()`, אחריו `get_window()`, ואז צילום חדש.
- modal שלא נראה: `list_windows()` ולאחר מכן צילום חלון האב; אין להמשיך עם קואורדינטות ישנות.
- `node_repl exec context not found`: מאפסים את kernel. אם השגיאה קשורה להצגת צילום, משתמשים ב־raw transport המתועד לעיל. אחרי איפוס יש ליצור צילום חדש; `screenshotId` ישן אינו תקף.
- תוצאת קלט לא ידועה: לא חוזרים על הקלט. צופים מחדש ובודקים מה קרה.
- desktop נעול: עוצרים ומבקשים מהמשתמש לפתוח אותו.

## 9. סיום בדיקה

1. מבטלים דיאלוגים פתוחים שאינם נחוצים.
2. לא שומרים ניסוי זמני שלא נועד להישמר.
3. סוגרים רק חלונות ותהליכים שנפתחו עבור ה־smoke; סגירה דרך UI אינה מחיקה, אך אם יש אזהרת unsaved changes בוחרים Discard רק לנתוני smoke.
4. מוודאים שה־API וה־containers נשארו בריאים.
5. מתעדים: מה נבדק בפועל, מה נבדק אוטומטית, אילו בעיות תוקנו, ומה מחייב בדיקת אדם/חומרה.
