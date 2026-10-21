"""Localised messages for the most-used output (summaries, the accuracy table, prompts, the stats report).

English is the source language and the fallback for any missing key, so partial translations are safe. Choose the
language with ``--lang``, ``MHBM_LANG`` or the usual ``LC_ALL``/``LC_MESSAGES``/``LANG`` variables. For right-to-left
languages each line that contains Arabic-script text is wrapped in Unicode direction isolates (disable with
``MHBM_BIDI=off``), which makes terminals that implement the bidirectional algorithm show it correctly.

The non-English catalogs were written with care but not by professional translators: corrections are welcome.
"""
from __future__ import annotations

import os
import re

RTL = {"fa", "ar"}
_ARABIC = re.compile("[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_lang = "en"

MESSAGES: dict = {
    "en": {
        "dry_run_apply": "dry run; add --apply to save (a snapshot and an undo entry are written first)",
        "imported": "{src}: read {read}, imported {imported}, skipped {skipped} exact repeats/already present (library now {library}). Next: `stats`, `clean-urls`, `dedupe`, `check`, `organize`.",
        "dup_summary": "{n} duplicates in {g} groups ({level} match)",
        "removed_remain": "removed {n}; {left} remain (undo with `undo`)",
        "link_status": "link status: {status}",
        "offline_warning": "NO LINK ANSWERED in this batch ({n} tried; e.g. {err}). The network, a firewall or a proxy is probably blocking requests, so nothing was recorded. Fix connectivity and re-run.",
        "prune_summary": "{n} bookmarks match status {status}",
        "prune_removed": "removed {n} (undo with `undo`)",
        "organize_model": "model: {state}; answering when at least {pct} likely to be right",
        "model_ready": "ready",
        "model_rules_only": "not enough filed bookmarks to learn from (rules only)",
        "classified": "{n} bookmarks classified ({sources}; {levels}). The rest need your judgement: `ai-batch` lists them with the model's best guesses.",
        "learn_title": "Filing model trained on {n} bookmarks in {f} folders.",
        "learn_measured": "Measured the way it will be used: bookmarks saved in a different sitting than the examples it learned from.",
        "learn_top": "best single guess right: {top1}   correct folder among its top 3: {top3}",
        "learn_table_head": "answer only when this likely to be right ->  exact folder | one level up | two levels up  (share of bookmarks, precision)",
        "learn_hint": "Raise the bar with `organize --precision 0.9`; allow coarser answers with `--coarse`. Unsure ones go to `ai-batch`.",
        "answered": "answered",
        "changes_saved": "saved (undo with `undo`)",
        "undid": "undid #{op} ({command}): restored {restored}, removed {removed}",
        "results": "{n} results",
        "doctor_summary": "{ok} ok, {warn} warnings, {fail} failures",
        "wizard_apply": "Apply '{name}'?",
        "wizard_stopped": "stopped; everything applied so far can be undone with `mhbm undo`",
        "progress_checking": "checking",
        "progress_archiving": "archiving",
        "report_total": "Bookmarks: {total}   archived/searchable: {archived}",
        "report_unfiled": "Unfiled: {unfiled}   Untagged: {untagged}   Untitled: {untitled}",
        "report_dups": "Duplicates: {extra} extra copies in {groups} groups",
        "report_errtitles": "Saved while the site was down (error-page titles): {n}",
        "report_folders": "Folders: deepest {depth} levels, {empty} empty",
        "report_top_folders": "Top folders:",
        "report_top_domains": "Top domains:",
        "report_top_tags": "Top tags:",
        "none": "(none)",
    },
    "fa": {
        "dry_run_apply": "اجرای آزمایشی؛ برای ذخیره‌سازی --apply را اضافه کنید (پیش از آن یک نسخهٔ پشتیبان و یک ورودی بازگردانی ثبت می‌شود)",
        "imported": "{src}: {read} مورد خوانده شد، {imported} مورد وارد شد، {skipped} مورد تکراری یا موجود نادیده گرفته شد (کتابخانه اکنون {library} نشانک دارد). مرحلهٔ بعد: `stats`، `clean-urls`، `dedupe`، `check`، `organize`.",
        "dup_summary": "{n} مورد تکراری در {g} گروه (تطابق {level})",
        "removed_remain": "{n} مورد حذف شد؛ {left} مورد باقی ماند (با `undo` بازگردانی کنید)",
        "link_status": "وضعیت پیوندها: {status}",
        "offline_warning": "هیچ پیوندی در این دسته پاسخ نداد (از {n} مورد؛ مثلاً {err}). احتمالاً شبکه، فایروال یا پروکسی درخواست‌ها را مسدود کرده است؛ بنابراین چیزی ثبت نشد. اتصال را درست کنید و دوباره اجرا کنید.",
        "prune_summary": "{n} نشانک با وضعیت {status} مطابقت دارد",
        "prune_removed": "{n} مورد حذف شد (با `undo` بازگردانی کنید)",
        "organize_model": "مدل: {state}؛ فقط وقتی حداقل {pct} احتمال درستی دارد پاسخ می‌دهد",
        "model_ready": "آماده",
        "model_rules_only": "نشانک دسته‌بندی‌شدهٔ کافی برای یادگیری نیست (فقط قانون‌ها)",
        "classified": "{n} نشانک دسته‌بندی شد ({sources}؛ {levels}). بقیه به قضاوت شما نیاز دارند: `ai-batch` آن‌ها را همراه با بهترین حدس‌های مدل فهرست می‌کند.",
        "learn_title": "مدل دسته‌بندی روی {n} نشانک در {f} پوشه آموزش دید.",
        "learn_measured": "به همان شکلی که در عمل به کار می‌رود اندازه‌گیری شد: نشانک‌هایی که در نشستی متفاوت از نمونه‌های آموزشی ذخیره شده‌اند.",
        "learn_top": "بهترین حدس درست است: {top1}   پوشهٔ درست در میان ۳ حدس برتر: {top3}",
        "learn_table_head": "فقط وقتی پاسخ بده که این‌قدر احتمال درستی دارد ->  همان پوشه | یک سطح بالاتر | دو سطح بالاتر  (سهم نشانک‌ها، دقت)",
        "learn_hint": "با `organize --precision 0.9` سطح را بالاتر ببرید؛ با `--coarse` پاسخ‌های کلی‌تر را مجاز کنید. موارد نامطمئن به `ai-batch` می‌روند.",
        "answered": "پاسخ داده شد",
        "changes_saved": "ذخیره شد (با `undo` بازگردانی کنید)",
        "undid": "#{op} ({command}) بازگردانده شد: {restored} مورد برگشت، {removed} مورد حذف شد",
        "results": "{n} نتیجه",
        "doctor_summary": "{ok} سالم، {warn} هشدار، {fail} خطا",
        "wizard_apply": "«{name}» اعمال شود؟",
        "wizard_stopped": "متوقف شد؛ هر آنچه تاکنون اعمال شده با `mhbm undo` قابل بازگردانی است",
        "progress_checking": "بررسی",
        "progress_archiving": "بایگانی",
        "report_total": "نشانک‌ها: {total}   بایگانی‌شده/قابل جستجو: {archived}",
        "report_unfiled": "بدون پوشه: {unfiled}   بدون برچسب: {untagged}   بدون عنوان: {untitled}",
        "report_dups": "تکراری‌ها: {extra} نسخهٔ اضافه در {groups} گروه",
        "report_errtitles": "ذخیره‌شده هنگام خرابی سایت (عنوان صفحهٔ خطا): {n}",
        "report_folders": "پوشه‌ها: عمیق‌ترین {depth} سطح، {empty} خالی",
        "report_top_folders": "پرتعدادترین پوشه‌ها:",
        "report_top_domains": "پرتعدادترین دامنه‌ها:",
        "report_top_tags": "پرتعدادترین برچسب‌ها:",
        "none": "(هیچ)",
    },
    "ar": {
        "dry_run_apply": "تشغيل تجريبي؛ أضف --apply للحفظ (تُنشأ نسخة احتياطية وسجل تراجع أولًا)",
        "imported": "{src}: قُرئ {read}، واستُورد {imported}، وتم تخطي {skipped} مكرر أو موجود (المكتبة الآن {library}). التالي: `stats` و`clean-urls` و`dedupe` و`check` و`organize`.",
        "dup_summary": "{n} مكرر في {g} مجموعة (مطابقة {level})",
        "removed_remain": "حُذف {n}؛ وبقي {left} (للتراجع: `undo`)",
        "link_status": "حالة الروابط: {status}",
        "offline_warning": "لم يستجب أي رابط في هذه الدفعة (جُرّب {n}؛ مثل {err}). على الأرجح أن الشبكة أو جدارًا ناريًا أو وكيلًا يحجب الطلبات، لذا لم يُسجَّل شيء. أصلح الاتصال وأعد المحاولة.",
        "prune_summary": "{n} إشارة مرجعية بحالة {status}",
        "prune_removed": "حُذف {n} (للتراجع: `undo`)",
        "organize_model": "النموذج: {state}؛ يجيب فقط عندما تكون احتمالية صحة الإجابة {pct} على الأقل",
        "model_ready": "جاهز",
        "model_rules_only": "لا توجد إشارات مصنّفة كافية للتعلّم (القواعد فقط)",
        "classified": "صُنّفت {n} إشارة مرجعية ({sources}؛ {levels}). الباقي يحتاج إلى حكمك: يعرضها `ai-batch` مع أفضل تخمينات النموذج.",
        "learn_title": "دُرّب نموذج التصنيف على {n} إشارة مرجعية في {f} مجلدًا.",
        "learn_measured": "قيس بالطريقة التي سيُستخدم بها: إشارات حُفظت في جلسة مختلفة عن الأمثلة التي تعلّم منها.",
        "learn_top": "أفضل تخمين صحيح: {top1}   المجلد الصحيح ضمن أفضل 3 تخمينات: {top3}",
        "learn_table_head": "أجب فقط عندما تكون الإجابة صحيحة بهذا القدر ->  المجلد نفسه | مستوى أعلى | مستويان أعلى  (نسبة الإشارات، الدقة)",
        "learn_hint": "ارفع المعيار بـ `organize --precision 0.9`؛ واسمح بإجابات أعمّ بـ `--coarse`. غير المؤكَّد يذهب إلى `ai-batch`.",
        "answered": "أُجيب",
        "changes_saved": "تم الحفظ (للتراجع: `undo`)",
        "undid": "تم التراجع عن #{op} ({command}): استُعيد {restored}، وحُذف {removed}",
        "results": "{n} نتيجة",
        "doctor_summary": "{ok} سليم، {warn} تحذيرات، {fail} أخطاء",
        "wizard_apply": "تطبيق «{name}»؟",
        "wizard_stopped": "توقف؛ يمكن التراجع عن كل ما طُبّق حتى الآن بـ `mhbm undo`",
        "progress_checking": "فحص",
        "progress_archiving": "أرشفة",
        "report_total": "الإشارات المرجعية: {total}   المؤرشفة/القابلة للبحث: {archived}",
        "report_unfiled": "بلا مجلد: {unfiled}   بلا وسوم: {untagged}   بلا عنوان: {untitled}",
        "report_dups": "المكررات: {extra} نسخة زائدة في {groups} مجموعة",
        "report_errtitles": "حُفظت أثناء تعطل الموقع (عناوين صفحات الخطأ): {n}",
        "report_folders": "المجلدات: أعمق {depth} مستويات، {empty} فارغة",
        "report_top_folders": "أكثر المجلدات:",
        "report_top_domains": "أكثر النطاقات:",
        "report_top_tags": "أكثر الوسوم:",
        "none": "(لا شيء)",
    },
    "es": {
        "dry_run_apply": "simulación; añade --apply para guardar (antes se crean una copia y una entrada para deshacer)",
        "imported": "{src}: leídos {read}, importados {imported}, omitidos {skipped} repetidos o ya presentes (la biblioteca tiene ahora {library}). Siguiente: `stats`, `clean-urls`, `dedupe`, `check`, `organize`.",
        "dup_summary": "{n} duplicados en {g} grupos (coincidencia {level})",
        "removed_remain": "eliminados {n}; quedan {left} (deshaz con `undo`)",
        "link_status": "estado de los enlaces: {status}",
        "offline_warning": "NINGÚN ENLACE RESPONDIÓ en este lote (se probaron {n}; p. ej. {err}). Probablemente la red, un cortafuegos o un proxy bloquea las peticiones, así que no se registró nada. Arregla la conexión y vuelve a ejecutar.",
        "prune_summary": "{n} marcadores con estado {status}",
        "prune_removed": "eliminados {n} (deshaz con `undo`)",
        "organize_model": "modelo: {state}; responde solo cuando es al menos {pct} probable que acierte",
        "model_ready": "listo",
        "model_rules_only": "no hay suficientes marcadores clasificados para aprender (solo reglas)",
        "classified": "{n} marcadores clasificados ({sources}; {levels}). El resto requiere tu criterio: `ai-batch` los lista con las mejores sugerencias del modelo.",
        "learn_title": "Modelo de clasificación entrenado con {n} marcadores en {f} carpetas.",
        "learn_measured": "Medido tal como se usará: marcadores guardados en una sesión distinta de los ejemplos de los que aprendió.",
        "learn_top": "mejor sugerencia acertada: {top1}   carpeta correcta entre las 3 primeras: {top3}",
        "learn_table_head": "responder solo cuando sea tan probable ->  carpeta exacta | un nivel más arriba | dos niveles más arriba  (proporción de marcadores, precisión)",
        "learn_hint": "Sube el listón con `organize --precision 0.9`; permite respuestas más generales con `--coarse`. Lo dudoso va a `ai-batch`.",
        "answered": "respondidos",
        "changes_saved": "guardado (deshaz con `undo`)",
        "undid": "deshecho #{op} ({command}): restaurados {restored}, eliminados {removed}",
        "results": "{n} resultados",
        "doctor_summary": "{ok} correctos, {warn} avisos, {fail} fallos",
        "wizard_apply": "¿Aplicar «{name}»?",
        "wizard_stopped": "detenido; todo lo aplicado hasta ahora se puede deshacer con `mhbm undo`",
        "progress_checking": "comprobando",
        "progress_archiving": "archivando",
        "report_total": "Marcadores: {total}   archivados/buscables: {archived}",
        "report_unfiled": "Sin carpeta: {unfiled}   Sin etiquetas: {untagged}   Sin título: {untitled}",
        "report_dups": "Duplicados: {extra} copias de más en {groups} grupos",
        "report_errtitles": "Guardados mientras el sitio fallaba (títulos de páginas de error): {n}",
        "report_folders": "Carpetas: la más profunda {depth} niveles, {empty} vacías",
        "report_top_folders": "Carpetas principales:",
        "report_top_domains": "Dominios principales:",
        "report_top_tags": "Etiquetas principales:",
        "none": "(ninguno)",
    },
    "fr": {
        "dry_run_apply": "simulation ; ajoutez --apply pour enregistrer (une copie et une entrée d'annulation sont créées d'abord)",
        "imported": "{src} : {read} lus, {imported} importés, {skipped} doublons ou déjà présents ignorés (la bibliothèque compte maintenant {library}). Ensuite : `stats`, `clean-urls`, `dedupe`, `check`, `organize`.",
        "dup_summary": "{n} doublons dans {g} groupes (correspondance {level})",
        "removed_remain": "{n} supprimés ; {left} restants (annulez avec `undo`)",
        "link_status": "état des liens : {status}",
        "offline_warning": "AUCUN LIEN N'A RÉPONDU dans ce lot ({n} essayés ; p. ex. {err}). Le réseau, un pare-feu ou un proxy bloque probablement les requêtes ; rien n'a donc été enregistré. Rétablissez la connexion et relancez.",
        "prune_summary": "{n} favoris avec l'état {status}",
        "prune_removed": "{n} supprimés (annulez avec `undo`)",
        "organize_model": "modèle : {state} ; ne répond que s'il est probable à au moins {pct} d'avoir raison",
        "model_ready": "prêt",
        "model_rules_only": "pas assez de favoris classés pour apprendre (règles seulement)",
        "classified": "{n} favoris classés ({sources} ; {levels}). Le reste demande votre jugement : `ai-batch` les liste avec les meilleures suggestions du modèle.",
        "learn_title": "Modèle de classement entraîné sur {n} favoris dans {f} dossiers.",
        "learn_measured": "Mesuré comme il sera utilisé : favoris enregistrés lors d'une session différente de celle des exemples appris.",
        "learn_top": "meilleure suggestion juste : {top1}   bon dossier parmi les 3 premiers : {top3}",
        "learn_table_head": "ne répondre que si c'est aussi probable ->  dossier exact | un niveau au-dessus | deux niveaux au-dessus  (part des favoris, précision)",
        "learn_hint": "Relevez le seuil avec `organize --precision 0.9` ; autorisez des réponses plus larges avec `--coarse`. Les cas incertains vont à `ai-batch`.",
        "answered": "traités",
        "changes_saved": "enregistré (annulez avec `undo`)",
        "undid": "annulé #{op} ({command}) : {restored} restaurés, {removed} supprimés",
        "results": "{n} résultats",
        "doctor_summary": "{ok} corrects, {warn} avertissements, {fail} échecs",
        "wizard_apply": "Appliquer « {name} » ?",
        "wizard_stopped": "arrêté ; tout ce qui a été appliqué peut être annulé avec `mhbm undo`",
        "progress_checking": "vérification",
        "progress_archiving": "archivage",
        "report_total": "Favoris : {total}   archivés/consultables : {archived}",
        "report_unfiled": "Sans dossier : {unfiled}   Sans étiquette : {untagged}   Sans titre : {untitled}",
        "report_dups": "Doublons : {extra} copies en trop dans {groups} groupes",
        "report_errtitles": "Enregistrés pendant une panne du site (titres de pages d'erreur) : {n}",
        "report_folders": "Dossiers : le plus profond {depth} niveaux, {empty} vides",
        "report_top_folders": "Dossiers principaux :",
        "report_top_domains": "Domaines principaux :",
        "report_top_tags": "Étiquettes principales :",
        "none": "(aucun)",
    },
    "de": {
        "dry_run_apply": "Probelauf; mit --apply speichern (vorher werden eine Sicherung und ein Rückgängig-Eintrag angelegt)",
        "imported": "{src}: {read} gelesen, {imported} importiert, {skipped} Wiederholungen/bereits vorhanden übersprungen (Bibliothek jetzt {library}). Weiter: `stats`, `clean-urls`, `dedupe`, `check`, `organize`.",
        "dup_summary": "{n} Duplikate in {g} Gruppen (Übereinstimmung {level})",
        "removed_remain": "{n} entfernt; {left} übrig (rückgängig mit `undo`)",
        "link_status": "Link-Status: {status}",
        "offline_warning": "KEIN LINK HAT GEANTWORTET in diesem Durchlauf ({n} versucht; z. B. {err}). Wahrscheinlich blockiert das Netzwerk, eine Firewall oder ein Proxy die Anfragen, daher wurde nichts gespeichert. Verbindung prüfen und erneut starten.",
        "prune_summary": "{n} Lesezeichen mit Status {status}",
        "prune_removed": "{n} entfernt (rückgängig mit `undo`)",
        "organize_model": "Modell: {state}; antwortet nur, wenn es mindestens zu {pct} wahrscheinlich richtig liegt",
        "model_ready": "bereit",
        "model_rules_only": "nicht genug einsortierte Lesezeichen zum Lernen (nur Regeln)",
        "classified": "{n} Lesezeichen eingeordnet ({sources}; {levels}). Der Rest braucht Ihr Urteil: `ai-batch` listet ihn mit den besten Vorschlägen des Modells.",
        "learn_title": "Einordnungsmodell mit {n} Lesezeichen in {f} Ordnern trainiert.",
        "learn_measured": "So gemessen, wie es eingesetzt wird: Lesezeichen aus einer anderen Sitzung als die gelernten Beispiele.",
        "learn_top": "beste Einzelvermutung richtig: {top1}   richtiger Ordner unter den ersten 3: {top3}",
        "learn_table_head": "nur antworten, wenn es so wahrscheinlich stimmt ->  genauer Ordner | eine Ebene höher | zwei Ebenen höher  (Anteil der Lesezeichen, Genauigkeit)",
        "learn_hint": "Mit `organize --precision 0.9` die Messlatte erhöhen; mit `--coarse` gröbere Antworten erlauben. Unsicheres geht an `ai-batch`.",
        "answered": "beantwortet",
        "changes_saved": "gespeichert (rückgängig mit `undo`)",
        "undid": "#{op} ({command}) rückgängig gemacht: {restored} wiederhergestellt, {removed} entfernt",
        "results": "{n} Ergebnisse",
        "doctor_summary": "{ok} in Ordnung, {warn} Warnungen, {fail} Fehler",
        "wizard_apply": "„{name}“ anwenden?",
        "wizard_stopped": "angehalten; alles bisher Angewendete lässt sich mit `mhbm undo` zurücknehmen",
        "progress_checking": "Prüfe",
        "progress_archiving": "Archiviere",
        "report_total": "Lesezeichen: {total}   archiviert/durchsuchbar: {archived}",
        "report_unfiled": "Ohne Ordner: {unfiled}   Ohne Tags: {untagged}   Ohne Titel: {untitled}",
        "report_dups": "Duplikate: {extra} überzählige Kopien in {groups} Gruppen",
        "report_errtitles": "Gespeichert, während die Seite gestört war (Fehlerseiten-Titel): {n}",
        "report_folders": "Ordner: tiefster {depth} Ebenen, {empty} leer",
        "report_top_folders": "Größte Ordner:",
        "report_top_domains": "Häufigste Domains:",
        "report_top_tags": "Häufigste Tags:",
        "none": "(keine)",
    },
}
LANGUAGES = tuple(MESSAGES)


def detect(explicit=None) -> str:
    for raw in (explicit, os.environ.get("MHBM_LANG"), os.environ.get("LC_ALL"), os.environ.get("LC_MESSAGES"), os.environ.get("LANG")):
        if not raw:
            continue
        code = re.split(r"[._@-]", raw.strip().lower())[0]
        if code in MESSAGES:
            return code
        if code in ("c", "posix"):
            return "en"
    return "en"


def set_language(explicit=None) -> str:
    global _lang
    _lang = detect(explicit)
    return _lang


def language() -> str:
    return _lang


def t(key: str, **kw) -> str:
    """Message ``key`` in the current language (English when missing), formatted with ``kw``."""
    text = MESSAGES.get(_lang, {}).get(key) or MESSAGES["en"][key]
    return text.format(**kw) if kw else text


def bidi(line: str) -> str:
    """Wrap a line that contains Arabic-script text in right-to-left isolates (for RTL languages only)."""
    if _lang in RTL and os.environ.get("MHBM_BIDI", "wrap") != "off" and _ARABIC.search(line):
        return "⁧" + line + "⁩"
    return line
