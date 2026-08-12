import html
import re

VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

ALLOWED_VARIABLES = {
    "fio", "first_name", "position",
    "birthday.day", "birthday.month", "birthday.date",
    "colleague.nom", "colleague.gen", "colleague.dat", "colleague.acc", "colleague.inst",
    "pronoun.nom", "pronoun.gen", "pronoun.dat", "pronoun.acc", "pronoun.inst",
    "celebrant.nom",
}

def grammar_context(gender):
    if gender == "female":
        return {
            "colleague.nom":"наша коллега","colleague.gen":"нашей коллеги",
            "colleague.dat":"нашей коллеге","colleague.acc":"нашу коллегу",
            "colleague.inst":"нашей коллегой","pronoun.nom":"она","pronoun.gen":"ее",
            "pronoun.dat":"ей","pronoun.acc":"ее","pronoun.inst":"ей",
            "celebrant.nom":"именинница",
        }
    if gender == "male":
        return {
            "colleague.nom":"наш коллега","colleague.gen":"нашего коллеги",
            "colleague.dat":"нашему коллеге","colleague.acc":"нашего коллегу",
            "colleague.inst":"нашим коллегой","pronoun.nom":"он","pronoun.gen":"его",
            "pronoun.dat":"ему","pronoun.acc":"его","pronoun.inst":"им",
            "celebrant.nom":"именинник",
        }
    return {
        "colleague.nom":"наш коллега","colleague.gen":"нашего коллеги",
        "colleague.dat":"нашему коллеге","colleague.acc":"нашего коллегу",
        "colleague.inst":"нашим коллегой","pronoun.nom":"он/она","pronoun.gen":"его/ее",
        "pronoun.dat":"ему/ей","pronoun.acc":"его/ее","pronoun.inst":"им/ей",
        "celebrant.nom":"именинник",
    }

def variable_context(employee, position=""):
    fio = employee.get("fio", "")
    parts = fio.split()
    first_name = parts[1] if len(parts) >= 2 else fio
    months = {1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",
              7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря"}
    day = employee.get("birthday_day", "")
    month = months.get(employee.get("birthday_month"), "")
    ctx = {
        "fio": fio, "first_name": first_name, "position": position,
        "birthday.day": str(day), "birthday.month": month,
        "birthday.date": f"{day} {month}".strip(),
    }
    ctx.update(grammar_context(employee.get("gender", "unknown")))
    return ctx

def validate_template(template):
    return sorted(set(VARIABLE_RE.findall(template)) - ALLOWED_VARIABLES)

def render_text(template, ctx):
    return VARIABLE_RE.sub(lambda m: ctx.get(m.group(1), m.group(0)), template)

def email_html(
    intro_text,
    wish_text="",
    position="",
    gender="unknown",
    card_src="",
    card_width=0,
):
    if gender == "female":
        accent, text_color, bg = "#a84269", "#64364a", "#fff8fb"
    elif gender == "male":
        accent, text_color, bg = "#315b86", "#28394d", "#f7f9fc"
    else:
        accent, text_color, bg = "#555555", "#333333", "#fafafa"

    position_block = ""

    wish_block = ""
    if wish_text:
        wish_block = (
            f'<tr><td style="padding:0 38px 28px 38px;font-size:24px;line-height:1.6;'
            f'color:{text_color};">{html.escape(wish_text)}</td></tr>'
        )

    card_block = ""
    if card_src:
        safe_src = html.escape(card_src, quote=True)
        width = int(card_width or 780)
        card_block = (
            '<tr><td align="center" style="padding:6px 18px 34px 18px;">'
            f'<img src="{safe_src}" width="{width}" alt="Поздравительная открытка" '
            f'style="display:block;width:100%;max-width:{width}px;height:auto;border:0;outline:none;text-decoration:none;">'
            '</td></tr>'
        )

    return f"""<!doctype html>
<html>
<body style="margin:0;padding:0;background:{bg};font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{bg};">
<tr><td align="center" style="padding:24px 12px;">
<table width="960" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:960px;background:#ffffff;">
<tr><td style="padding:32px 38px 16px 38px;font-size:24px;line-height:1.55;color:{text_color};">Добрый день!</td></tr>
<tr><td style="padding:6px 38px 24px 38px;font-size:27px;line-height:1.55;color:{text_color};">{html.escape(intro_text)}</td></tr>
{position_block}
{wish_block}
{card_block}
</table>
</td></tr>
</table>
</body>
</html>"""

