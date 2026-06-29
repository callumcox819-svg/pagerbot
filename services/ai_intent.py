"""Classify client messages — rules first, optional OpenAI."""

from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    INTERESTED = "interested"
    POSITIVE = "positive"
    READY = "ready"
    JOINED = "joined"
    DEPOSIT_DONE = "deposit_done"
    COMPLAINT = "complaint"
    QUESTION = "question"
    IMAGE_ONLY = "image_only"
    GAME_ID_TEXT = "game_id_text"
    MONEY_REQUEST = "money_request"
    PHONE_REQUEST = "phone_request"
    DECLINED = "declined"
    UNKNOWN = "unknown"


MONEY_REFUSAL_AR = (
    "نحن لا نُعطي أموالاً، بل نساعدك فقط على الكسب بالتكتيكات."
)
MONEY_REFUSAL_EN = (
    "We don't give money — we only help you earn with our tactics."
)
MONEY_REFUSAL_FR = (
    "Nous ne donnons pas d'argent — nous vous aidons seulement à gagner avec nos tactiques."
)

_MONEY_REQUEST = re.compile(
    r"\b(send(ing)?\s+me\s+(money|cash|funds)|give\s+me\s+(money|cash|funds)|"
    r"lend\s+me|loan\s+me|need\s+money|want\s+money|"
    r"skick.*(money|cash))\b",
    re.I,
)
_AR_MONEY_REQUEST = re.compile(
    r"فلوس|فلوسي|فلوسك|"
    r"ارسل(ي|ني|ولي)|ابعت(لي|ولي)|حول(ي|لي)|"
    r"محتاج\s*فلوس|عايز\s*فلوس|عاوز\s*فلوس|"
    r"ساعدني\s*بفلوس|اديني\s*فلوس|اعطني\s*فلوس"
)
_FR_MONEY_REQUEST = re.compile(
    r"\b("
    r"prête[- ]moi|pret[- ]moi|"
    r"donne[- ](moi )?(de l')?argent|"
    r"envoy\w*[- ](moi )?(de l')?argent|"
    r"besoin d['']?argent|besoin d argent"
    r")\b",
    re.I,
)
_FR_LINK_ASK = re.compile(
    r"\benvoy\w*.*\blien\b|\blien\b.*\benvoy\w*\b",
    re.I,
)
_FR_WHAT_REQUIRED = re.compile(
    r"\b(que faire|quoi faire|qu'est-ce qu'il faut|quest ce qu il faut|"
    r"c'est quoi|cest quoi|il me faut quoi)\b",
    re.I,
)
_FR_POST_LINK = re.compile(
    r"\b(et après|et apres|prochaine étape|prochaine etape|"
    r"comment déposer|comment deposer|déposer|deposer|"
    r"vous êtes inscrit|etes vous inscrit|êtes-vous inscrit|inscrit\??)\b",
    re.I,
)
_XBET_BRAND = re.compile(r"\b(1xbet|1x\s*bet|xbet|1хбет|1х\s*бет)\b", re.I)
_XBET_SITE_QUESTION = re.compile(
    r"\b("
    r"ce site|cet site|this site|the site|ce lien|this link|le lien|"
    r"c'est quoi|cest quoi|c'est bien|cest bien|is it|is this|"
    r"m'envoi|m'envoie|me envoie|me envoi|envoie vers|envoi vers|"
    r"redirige|redirect|sur 1x|vers 1x|to 1x|"
    r"hein|n'est-ce pas|nest ce pas|right|correct"
    r")\b",
    re.I,
)
_FR_INTERESTED = re.compile(
    r"\b(intéressé|interesse|intéressée|interessee|explique|expliquez|"
    r"comment ça marche|comment ca marche|je veux|dites-moi|dites moi)\b",
    re.I,
)
_FR_POSITIVE = re.compile(
    r"\b(oui|ouii+|d'accord|daccord|ok|okay|bien sûr|bien sur|"
    r"je suis partant|d'acc|volontiers|avec plaisir|"
    r"parfait|super|génial|genial|nickel|ça marche|ca marche|"
    r"c'est bon|cest bon|très bien|tres bien|entendu|"
    r"comme tu veux|je suis d'accord|why not|pas de problème|pas de probleme)\b",
    re.I,
)
_FR_GREETING = re.compile(r"\b(bonjour|bonsoir|salut|coucou|bjr)\b", re.I)
_FR_REFUSAL = re.compile(
    r"\b(non|nn|nop|nan|jamais|stop|laisse|"
    r"pas intéressé|pas interesse|pas envie|"
    r"je veux pas|j veux pas|j' veux pas|veux pas|"
    r"non merci|nn merci|merci non|"
    r"ça m'intéresse pas|ca m'interesse pas|"
    r"je ne veux pas|je veux plus|"
    r"ne m['']?écrivez pas|ne me écrivez pas)\b",
    re.I,
)
_EN_REFUSAL = re.compile(
    r"\b(no thanks|not interested|don'?t want|do not want|"
    r"no thank you|leave me alone|not for me)\b",
    re.I,
)
_FR_READY = re.compile(
    r"\b(je suis prêt|je suis pret|prêt à commencer|pret a commencer|"
    r"on commence|commençons|commencons|vas-y|vas y|"
    r"allez-y|allez y|allons-y|allons y|ok c'est bon|"
    r"prêt à continuer|pret a continuer|oui prêt|oui pret)\b",
    re.I,
)
_FR_REG = re.compile(
    r"\b(inscription|inscrit|lien|enregistrer|créer un compte|creer un compte|"
    r"envoy\w*.*lien|lien.*envoy\w*)\b",
    re.I,
)
_FRENCH_LATIN = re.compile(r"[\u00C0-\u024F]")
_AR_WHAT_REQUIRED = re.compile(
    r"ايه\s*المطلوب|إيه\s*المطلوب|ماذا\s*المطلوب|وش\s*المطلوب|"
    r"طب\s*ايه|ايه\s*اللي|إيه\s*اللي|ايه\s*المطلوب"
)
_INTERESTED = re.compile(
    r"\b(interested|interest|312|teach me|need help|need job|i am interested|"
    r"i'm interested|tell me more|am interested|kindly explain|explain it|"
    r"your help|help me|go ahead|hi go ahead|interested please|"
    r"i'm serious|i am serious|very interested|yess?\s+sir|"
    r"would like to join|would love to join|wanna join|want to join|"
    r"like to join|count me in|want to learn|i want to learn|learn how)\b",
    re.I,
)
_GREETING = re.compile(
    r"\b(good (morning|afternoon|evening)|hello|hi|hey)\b", re.I
)
_ACK = re.compile(
    r"\b(sure|done|ok|okay|thanks|thank you|i have done|as you said|"
    r"will do|already done|got it|understood|alright)\b",
    re.I,
)
_POSITIVE = re.compile(
    r"\b(yess?|ok|okay|explain|i am|you can|sure|alright|got it|"
    r"how can i start|how do i start|how to start)\b",
    re.I,
)
_READY = re.compile(
    r"\b(am ready|i'?m ready|let'?s start|start today|ready to start)\b", re.I
)
_JOINED = re.compile(r"\b(have joined|joined|i joined)\b", re.I)
_COMPLAINT = re.compile(
    r"\b(lost|losed|didn'?t win|scam|taking my money|stop|refund|nothing happened|lied)\b",
    re.I,
)
_GAME_ID = re.compile(r"\b17\d{6,}\b")
_GAME_ID_LEGACY = re.compile(r"\b16\d{6,}\b")
_GAME_ID_EG = re.compile(r"\b17\d{6,}\b")
_ARABIC = re.compile(r"[\u0600-\u06FF]")
_AR_INTERESTED = re.compile(
    r"مهتم|اهتم|عايز|عاوز|حابب|حابة|ابي|أبي|عاوزه|عايزه|محتاج|مساعد|ساعد|ممكن|"
    r"ارغب|أرغب|انضم|أنضم|انضمام|أنضمام|حابب اعرف|عاوز اعرف|"
    r"استثمر|أستثمر|استثمار|أريد أن|اريد ان|أريد الاستثمار|اريد الاستثمار|"
    r"أنا مهتم|انا مهتم"
)
_AR_JOIN_DETAILS = re.compile(
    r"اعمل\s*ايه|أعمل\s*ايه|اعمل\s*إيه|أعمل\s*إيه|"
    r"المطلوب|الفكره|الفكرة|اي\s+المطلوب|ايه\s+الفكره|إيه\s+الفكرة|"
    r"ايه\s+الموضوع|إيه\s+الموضوع|اي\s+الموضوع|أي\s+الموضوع|"
    r"عباره\s+عن|عبارة\s+عن|هتعمل\s+معنا|اى\s+الشغل|إيه\s+الشغل|"
    r"من\s+فين|منين|توضيح|توضيخ|تفاصيل|اشرح|فهمني|ازاي|إزاي|"
    r"انضمام\s+لي|الانضمام|مش\s+شغال|مش\s+فاهم|مش\s+فاهمة"
)
_AR_GREETING = re.compile(
    r"السلام|سلام|مرحب|أهلا|اهلا|هلا|صباح|مساء|ازيك|إزيك"
)
_POSITIVE_EMOJI = re.compile(
    r"^[\s"
    r"\U0001F44D\U0001F44C\U0001F44F\U0001F600-\U0001F64F"
    r"\U0001F970\U0001F60A\U0001F603\U00002764\U00002705\U0001F49D"
    r"\U0001F64F\U0001F4AF"
    r"👍👌❤✅🙏😊😄💯🙂😆❤️👏"
    r"]+$"
)


def is_positive_emoji_only(text: str) -> bool:
    """Thumbs-up / heart / smiley only — treat as yes after intro."""
    t = (text or "").strip()
    if not t:
        return False
    if t in (
        "👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿",
        "👌", "❤", "❤️", "💯", "🙂", "😊", "😄", "🙏", "👏", "✅",
    ):
        return True
    return bool(_POSITIVE_EMOJI.match(t))


def contains_positive_emoji(text: str) -> bool:
    """Short reply with 👍 / ✅ — e.g. «Ok 👍», «👍 oui»."""
    t = (text or "").strip()
    if not t:
        return False
    if is_positive_emoji_only(t):
        return True
    if len(t) > 20:
        return False
    if not re.search(r"[👍👌✅💯🙂😊🙏👏❤️❤]", t):
        return False
    letters = re.sub(
        r"[\s👍👌✅💯🙂😊🙏👏❤️❤\W]",
        "",
        t,
        flags=re.UNICODE,
    )
    return len(letters) <= 8


def is_positive_message_reaction(reaction: str | None) -> bool:
    """Pager/Facebook message.reaction field — like / love / 👍."""
    if not reaction:
        return False
    r = str(reaction).strip().lower()
    if r in ("like", "love", "care"):
        return True
    return is_positive_emoji_only(str(reaction).strip())


def is_reaction_only_message(
    text: str,
    attachments: list | None = None,
    *,
    message_reaction: str | None = None,
) -> bool:
    """Thumbs / FB like only — not verbal oui/ok (those go through funnel)."""
    t = (text or "").strip()
    if contains_positive_emoji(t):
        return True
    if not t and is_messenger_reaction_attachment(attachments or []):
        return True
    if not t and is_positive_message_reaction(message_reaction):
        return True
    return False


def _funnel_positive_max_step(geo: str = "zm") -> int:
    if geo == "cm":
        return 6
    if geo == "eg":
        return 5
    return 4


def is_messenger_reaction_attachment(attachments: list) -> bool:
    """Facebook like / sticker — not a deposit or ID screenshot."""
    for att in attachments or []:
        typ = (att.get("type") or "").lower()
        if typ in (
            "sticker",
            "like",
            "thumbs_up",
            "emoji",
            "fallback",
            "reaction",
        ):
            return True
        if typ == "image":
            payload = att.get("payload") or {}
            if payload.get("sticker_id") or att.get("sticker_id"):
                return True
            url = (payload.get("url") or "").lower()
            if any(
                x in url
                for x in (
                    "sticker",
                    "/t39.1997",
                    "reaction",
                    "like_thumb",
                    "thumbs",
                    "emoji.php",
                    "/images/emoji",
                    "static.xx.fbcdn.net/images/emoji",
                )
            ):
                return True
            w = payload.get("width") or att.get("width")
            h = payload.get("height") or att.get("height")
            try:
                if w and h and int(w) <= 200 and int(h) <= 200:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def _normalize_short_reply(text: str) -> str:
    t = re.sub(r"[^\w\s]", "", (text or "").strip())
    return re.sub(r"\s+", " ", t).strip().lower()


def is_short_affirmative(text: str) -> bool:
    """Oui / Wii / OK — short consent after intro or funnel question."""
    raw = (text or "").strip()
    if not raw:
        return False
    t = _normalize_short_reply(raw)
    if not t:
        return False
    if re.fullmatch(
        r"(wii+|wi+|oui+|ouii+|ouais|ouaip|ok+|okay|yep|yes+|yess|"
        r"daccord|dacc|cbien|cbon|cestbon|tres bien|très bien|"
        r"vasy|allonsy|allezy|chef|si|yup|"
        r"oui\s+exactement|exactement|"
        r"letsgo|letgo|go|sure|alright|parfait|super|nickel)",
        t,
        re.I,
    ):
        return True
    if re.fullmatch(r"oui\s+exactement\.?", raw.strip(), re.I):
        return True
    if len(t.split()) <= 2 and _FR_POSITIVE.search(raw):
        return True
    return False


def is_refusal_reply(text: str) -> bool:
    """Client refuses — do not send intro or funnel scripts."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"no\.?", t, re.I):
        return True
    if _EN_REFUSAL.search(t):
        return True
    if _FR_REFUSAL.search(t):
        return True
    if re.search(r"\bmerci\b", t, re.I) and re.search(
        r"\b(pas|non|nn|veux pas|want)\b", t, re.I
    ):
        return True
    return False


def is_funnel_positive_reaction(
    text: str,
    attachments: list | None = None,
    *,
    funnel_step: int = 0,
    geo: str = "zm",
    message_reaction: str | None = None,
) -> bool:
    """Early funnel — emoji / FB like / short oui means «yes, continue»."""
    if not (text or "").strip():
        if is_messenger_reaction_attachment(attachments or []):
            return True
        if is_positive_message_reaction(message_reaction):
            return True
    if funnel_step >= _funnel_positive_max_step(geo):
        return False
    if is_short_affirmative(text):
        return True
    if contains_positive_emoji(text):
        return True
    return False


_AR_POSITIVE = re.compile(
    r"تمام|أيوه|ايوه|ايو|آه|اه|اوك|نعم|يلا|بينا|ماشي|ماش|حاضر|طيب|كويس|"
    r"ياريت|اكيد|أكيد|طبعا|حلو|جميل|نجرب|موافق|معاك|"
    r"اتفضلي|اتفضلى|اتفضل|اي\s*حاجة|أي\s*حاجة|اي\s*حاجه|أي\s*حاجه"
)
_AR_DETAILS = re.compile(
    r"قولي|قول|تفاصيل|تفصيل|التفاصيل|التفصيل|نفاصيل|فهمني|اشرح|علمني|"
    r"ازاي|إزاي|ازاى|ازى|وضح|"
    r"أحتاج|احتاج|معلومات|معومات|اكثر|أكثر|"
    r"دخل\s*زياد|زياد[ةه]|اعمل\s*دخل|دخل\s*اكثر|زيادة\s*الدخل|"
    r"ازا[ىي].*دخل|ازاي.*دخل|إزاي.*دخل"
)
_AR_READY = re.compile(r"جاهز|هبدأ|نبدأ|ابدأ|جاهزين|يلا بينا")
_AR_REG_COMPLETE = re.compile(
    r"خلصت|سجلت|انهيت|انتهيت|عملت حساب|فتحت حساب|عملت التسجيل|خلصت التسجيل"
)
_AR_REG_PENDING = re.compile(
    r"لسه|مش خلصت|مش سجلت|بسجل|هسجل|بعمل|لسا|لسه مش|مش عملت"
)
_AR_DEFERRAL = re.compile(
    r"بكرة|كمان شويه|مش دلوقتي|بعدين|مش جاهز|مش دلوقت|بعدين"
)
_AR_DEPOSIT = re.compile(
    r"عملت إيداع|عملت ايداع|حطيت|ودعت|عملت ديبوزيت|"
    r"خلصت.*إيداع|خلصت.*ايداع|عملت.*إيداع|عملت.*ايداع"
)
_AR_DEPOSIT_QUESTION = re.compile(
    r"في ايه|فى ايه|فيم|بكم|كام|ازاي|إزاي|كيف|وين|فين|ايه|إيه|؟|\?|بالظبط|"
    r"how much|what.*deposit|where.*deposit|which.*deposit",
    re.I,
)
_AR_COMPLAINT = re.compile(r"نصب|كذب|خسارة|سرق|احتيال|غش")
_AR_APP = re.compile(
    r"تطبيق|متصفح|انزل|البرنامج|الابليكيشن|كروم|chrome|download|app"
)
_AR_REG_LINK = re.compile(
    r"فين.*(حساب|سجل|تسجيل|لينك|رابط)|"
    r"(اعمل|أعمل|عمل).*(حساب|تسجيل)|"
    r"(اللينك|الرابط|لينك|رابط)|"
    r"ازاي.*(سجل|تسجيل|حساب)"
)
_REGISTRATION_FOLLOWUP = re.compile(
    r"\b(explain|how can i start|how do i start|how to start|tell me how|"
    r"how does it work|how it works|what do i do|what should i do|"
    r"what is next|what's next|next step|get started|start now)\b",
    re.I,
)
_POST_LINK_QUESTION = re.compile(
    r"\b(what.*(next|come|after|happens|do i)|once.*(click|open|tap).*link|"
    r"after.*(link|click|register)|next step|what.*(deposit|register)|"
    r"how.*(deposit|register)|don't have.*\d+|what if you don't|"
    r"click the link)\b",
    re.I,
)
_DEFERRAL = re.compile(
    r"\b(let you know|when i'?m ready|not ready|maybe later|tomorrow|"
    r"next day|another day|only today|hand cash|unfortunately|"
    r"will tell you|get back to you|not now|not today|later on|"
    r"give me time|need time|only have)\b",
    re.I,
)
_FR_DEFERRAL = re.compile(
    r"\b(pas (maintenant|encore) d['']?argent|pas d['']?argent|pas l['']?argent|"
    r"n['']?ai pas d['']?argent|j'ai pas d['']?argent|"
    r"pas de connexion|connexion (n['']?est pas|ne marche)|"
    r"internet (est )?faible|pas la connexion|"
    r"pour le moment|plus tard|demain|"
    r"je peux rien|je ne peux pas|trop de pression)\b|"
    r"\bdéjà fait\b",
    re.I,
)
_REG_COMPLETE = re.compile(
    r"\b(registered|registration done|done registering|done with registration|"
    r"i registered|have registered|finished registering|signed up|account created|"
    r"created (my |an )?account|i have registered)\b",
    re.I,
)
_FR_REG_COMPLETE = re.compile(
    r"(déjà|deja).{0,20}(inscription|inscrit|enregistr)|"
    r"(inscription|inscrit).{0,12}(fait|faite|termin)|"
    r"compte.{0,16}(ouvert|1x|créé|cree|ouvre)|"
    r"(ouvert|ouverte).{0,12}(compte|account)|"
    r"\bje suis inscrit\b|"
    r"\bj'ai (créé|cree|ouvert).{0,12}compte\b",
    re.I,
)
_DEPOSIT_TIER = re.compile(
    r"^(2000|1000|500|300|200|100|50|30)\s*(?:djf|zmw|fr|f)?\.?$",
    re.I,
)
_TIER_TYPO = {
  # client often types profit (3 000) instead of deposit tier (300)
    3000: 300,
    5000: 500,
    10000: 1000,
    20000: 2000,
    370: 300,
}
_TIER_AMOUNT = re.compile(
    r"\b(2000|1000|500|300|200|100|50|30)\s*(?:djf|zmw|fr|f)?\b",
    re.I,
)


_PHONE_REQUEST = re.compile(
    r"\b(your|ur)\s+(phone\s*)?number\b|"
    r"\b(send|give|share|drop)\s+(me\s+)?(your\s+)?(phone\s*)?number\b|"
    r"\b(phone|mobile|cell)\s*number\b|"
    r"\bcall\s+me\b|"
    r"\bwhatsapp\s*(number|no\.?)?\b|"
    r"\bnumber\s+please\b",
    re.I,
)
_FR_PHONE_REQUEST = re.compile(
    r"\b(envoy\w*|donn\w*|pass\w*|mets?)\b.{0,30}\b(ton|votre|ta)\s*(numéro|numero)\b|"
    r"\b(numéro|numero)\s+(de\s+)?(téléphone|telephone|tel)\b|"
    r"\b(ton|votre|ta)\s+(numéro|numero|tel)\b|"
    r"\bappelle[- ]moi\b|"
    r"\bwhatsapp\b",
    re.I,
)
_AR_PHONE_REQUEST = re.compile(
    r"رقمك|رقمك؟|رقم الهاتف|رقم التليفون|رقم الواتس|"
    r"واتس|واتساب|اتصل(ي)?\s*بي|اتصل\s*معي|"
    r"ابعت(لي)?\s*رقم|ارسل(لي)?\s*رقم|رقمك\s*ايه|رقمك\s*إيه"
)


def is_money_request(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if is_funnel_earning_interest(t):
        return False
    if _AR_MONEY_REQUEST.search(t):
        return True
    if _FR_MONEY_REQUEST.search(t):
        return True
    return bool(_MONEY_REQUEST.search(t))


def is_funnel_earning_interest(text: str) -> bool:
    """«I want to learn how to make $1000» — funnel interest, not a loan request."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"money\??", t, re.I):
        return True
    return bool(
        re.search(
            r"(?i)\b("
            r"learn how|want to learn|i want to learn|how to (make|earn|start)|"
            r"make.{0,16}(per day|a day)|interested|need help|"
            r"earn.{0,16}money|help me understand"
            r")\b",
            t,
        )
    )


def is_phone_number_request(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _AR_PHONE_REQUEST.search(t):
        return True
    if _FR_PHONE_REQUEST.search(t):
        return True
    return bool(_PHONE_REQUEST.search(t))


def money_refusal_reply(text: str, *, geo: str = "zm") -> str:
    t = (text or "").strip()
    if geo == "eg" or _ARABIC.search(t):
        return MONEY_REFUSAL_AR
    if geo == "dj" or geo == "cm" or _FRENCH_LATIN.search(t):
        return MONEY_REFUSAL_FR
    return MONEY_REFUSAL_EN


def phone_chat_only_reply(text: str, *, geo: str = "zm") -> str:
    """Reply when client asks for phone / WhatsApp — language follows channel geo."""
    from services.script_engine import load_script

    g = (geo or "zm").strip().lower()
    if g not in ("zm", "eg", "dj", "cm"):
        g = "zm"
    try:
        return load_script(g, "extras/chat_only")
    except FileNotFoundError:
        pass
    fallbacks = {
        "eg": (
            "نتحدث فقط عبر الإنترنت في هذه المحادثة. "
            "إذا كنت مهتماً، أخبرني وسنكمل هنا."
        ),
        "cm": (
            "Nous communiquons uniquement en ligne dans ce chat. "
            "Si tu es intéressé(e), dis-le moi et on continue ici."
        ),
        "dj": (
            "Nous communiquons uniquement en ligne dans ce chat. "
            "Si tu es intéressé(e), dis-le moi et on continue ici."
        ),
        "zm": (
            "We only communicate online in this chat. "
            "If you're interested, let me know and we'll continue here."
        ),
    }
    return fallbacks.get(g, fallbacks["zm"])


def is_what_required_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _AR_WHAT_REQUIRED.search(t):
        return True
    if _FR_WHAT_REQUIRED.search(t):
        return True
    return is_post_link_registration_question(t)


def is_commitment_reply(text: str) -> bool:
    """Short affirmations after intro / ZMW table (e.g. Yes, I'm serious)."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"yes\.?", t, re.I):
        return True
    return bool(
        re.search(
            r"\b(i'?m serious|i am serious|very interested|interested please|"
            r"yes please|count me in|let'?s go|lets go|want to start|"
            r"please explain|tell me more|go ahead|i'?m in|sign me up|"
            r"i want in|ready when you are)\b",
            t,
            re.I,
        )
    )


def wants_registration_followup(text: str) -> bool:
    """After intro — treat start/how/explain questions like a positive reply."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_REG_LINK.search(t):
        return True
    if re.fullmatch(r"explain\??", t, re.I):
        return True
    if is_post_link_registration_question(t):
        return True
    return bool(_REGISTRATION_FOLLOWUP.search(t))


def is_post_link_registration_question(text: str) -> bool:
    """After reg link — client asks what to do next (send deposit script, not operator)."""
    t = (text or "").strip()
    if not t:
        return False
    if is_xbet_site_question(t):
        return True
    if _FR_POST_LINK.search(t):
        return True
    return bool(_POST_LINK_QUESTION.search(t))


def is_xbet_site_question(text: str) -> bool:
    """«Ce site m'envoie sur 1xbet?» — confirm platform + must register."""
    t = (text or "").strip()
    if not t:
        return False
    if _XBET_BRAND.search(t):
        if "?" in t or _XBET_SITE_QUESTION.search(t):
            return True
        if re.search(
            r"\b(sur|vers|to|on|loin|envoie|envoy|redir|redirect|mène|mene)\b",
            t,
            re.I,
        ) and len(t.split()) >= 4:
            return True
        return False
    if _XBET_SITE_QUESTION.search(t) and re.search(
        r"\b(bet|inscri|register|promo|code|site|lien|link)\b", t, re.I
    ):
        return True
    return False


def xbet_site_confirm_reply(*, geo: str = "zm") -> str:
    from services.script_engine import load_script

    g = (geo or "zm").strip().lower()
    if g not in ("zm", "eg", "dj", "cm"):
        g = "zm"
    try:
        return load_script(g, "extras/xbet_site_confirm")
    except FileNotFoundError:
        fallbacks = {
            "eg": "أيوه، عشان تستخدم كود العرض لازم تسجّل على الموقع.",
            "cm": (
                "Oui, pour utiliser le code promotionnel que nous vous avons fourni, "
                "vous devez vous inscrire."
            ),
            "dj": (
                "Oui, pour utiliser le code promotionnel que nous vous avons fourni, "
                "vous devez vous inscrire."
            ),
            "zm": (
                "Yes — to use the promotional code we gave you, "
                "you need to register on the site."
            ),
        }
        return fallbacks.get(g, fallbacks["zm"])


def wants_registration_link(text: str) -> bool:
    """Client asks where/how to register — send reg+link, not intro again."""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(
        r"(?i)\b(lequel|laquelle|quel|quelle|where|which)\b.{0,24}\blien\b",
        t,
    ):
        return True
    if re.search(
        r"(?i)\blien\b.{0,24}\b(lequel|laquelle|quel|quelle)\b",
        t,
    ):
        return True
    if _FR_LINK_ASK.search(t):
        return True
    if _FR_REG.search(t) and "lien" in t.lower():
        return True
    return bool(_AR_REG_LINK.search(t)) or wants_registration_followup(t)


def is_deferral_reply(text: str) -> bool:
    """Client postpones — do not send registration or game ID scripts."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"no\.?", t, re.I):
        return True
    if _AR_DEFERRAL.search(t):
        return True
    if _FR_DEFERRAL.search(t):
        return True
    return bool(_DEFERRAL.search(t))


def is_registration_complete(text: str) -> bool:
    """Client finished registration — OK to send deposit script."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_REG_COMPLETE.search(t):
        return True
    if _FR_REG_COMPLETE.search(t):
        return True
    return bool(_REG_COMPLETE.search(t))


def is_on_registration_site(text: str) -> bool:
    """Client opened the reg link / is on 1xbet."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(
        re.search(
            r"\b(1xbet|xbet|1x bet|on the site|opened the link|opening the link|"
            r"taking me|it'?s taking me|i'?m on|went to the link|"
            r"compte 1x|sur 1xbet|sur le site)\b",
            t,
            re.I,
        )
    )


def is_post_reg_ack(text: str) -> bool:
    """Short yes after reg link — treat as registered, send deposit script."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _AR_DETAILS.search(raw):
        return False
    if _AR_POSITIVE.search(raw) and len(raw.split()) <= 3:
        return True
    t = re.sub(r"[^\w\s]", "", raw)
    if not t:
        return False
    return bool(
        re.fullmatch(
            r"(yeah|yes|yep|yess|ok|okay|sure|alright|done|finished)\s*",
            t,
            re.I,
        )
    )


def is_registration_confirmed(text: str) -> bool:
    """After 04+05 — client registered or clearly on the site."""
    return (
        is_registration_complete(text)
        or is_on_registration_site(text)
        or is_post_reg_ack(text)
    )


def is_already_registered_before_funnel(text: str) -> bool:
    """Client was registered before our link — move to completed, no deposit funnel."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(
        re.search(
            r"(déjà|deja).{0,24}(inscription|inscrit|enregistr)|"
            r"(already|déjà|deja).{0,16}(registered|have an account)",
            t,
            re.I,
        )
    )


def is_deposit_tier_choice(text: str, *, geo: str = "zm") -> bool:
    """Answer to deposit tier table — treat as ready for registration."""
    t = (text or "").strip()
    if not t:
        return False
    if geo == "cm":
        if re.fullmatch(r"^(1000|1500)\s*(?:cfa|fr|f)?\.?$", t, re.I):
            return True
        if len(t.split()) <= 8 and re.search(
            r"\b(1000|1500)\s*(?:cfa|fr|f)?\b", t, re.I
        ):
            return True
        return False
    if _DEPOSIT_TIER.match(t):
        return True
    digits = re.sub(r"[^\d]", "", t)
    if digits.isdigit() and int(digits) in _TIER_TYPO:
        return True
    if len(t.split()) <= 12 and _TIER_AMOUNT.search(t):
        return True
    return False


def is_age_answer(text: str) -> bool:
    """CM funnel — client answers the age question."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"\d{1,2}", t):
        return 15 <= int(t) <= 99
    if re.search(r"\b(j'ai|jai|ai)\s*\d{1,2}\s*ans\b", t, re.I):
        return True
    if re.search(r"\b\d{1,2}\s*ans\b", t, re.I):
        return True
    return False


def wants_details_after_intro(text: str) -> bool:
    """After intro — client asks for explanation."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_DETAILS.search(t):
        return True
    return wants_registration_followup(t)


def is_app_or_browser_question(text: str) -> bool:
    """Client asks app vs browser — send 08_app_or_browser."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_APP.search(t):
        return True
    return bool(
        re.search(
            r"\b(app|browser|download|install|play store|apk)\b",
            t,
            re.I,
        )
    )


def is_ready_for_registration(text: str, *, geo: str = "zm") -> bool:
    """After 02+03 — client wants reg link (not vague okay / later)."""
    t = (text or "").strip()
    if not t or is_deferral_reply(t):
        return False
    if _AR_READY.search(t):
        return True
    if _AR_POSITIVE.search(t) and len(t.split()) <= 5 and not _AR_DETAILS.search(t):
        return True
    if is_deposit_tier_choice(t, geo=geo):
        return True
    if is_short_affirmative(t):
        return True
    if _FR_POSITIVE.search(t) and len(t.split()) <= 4:
        return True
    if re.fullmatch(r"yes\.?", t, re.I):
        return True
    if is_commitment_reply(t):
        return True
    if _READY.search(t):
        return True
    if wants_registration_followup(t):
        return True
    if _INTERESTED.search(t) and "explain" in t.lower():
        return True
    if re.fullmatch(r"explain\??", t, re.I):
        return True
    # Short ack only — not enough for registration link
    if _ACK.search(t) and len(t.split()) <= 4:
        return False
    if _POSITIVE.search(t) and not _DEFERRAL.search(t):
        if len(t.split()) > 5:
            return False
        return True
    return False


def is_deposit_question(text: str) -> bool:
    """«اعمل ايداع في ايه» — asking about deposit, not confirming."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_DEPOSIT_QUESTION.search(t) and re.search(
        r"إيداع|ايداع|deposit", t, re.I
    ):
        return True
    return bool(
        re.search(
            r"\b(how much|what (amount|to deposit)|where (do i|to) deposit|"
            r"must deposit|need to deposit|should i deposit)\b",
            t,
            re.I,
        )
    )


_READY_CONTINUE_BROADCAST = re.compile(
    r"prêt à continuer|pret a continuer",
    re.I,
)
_BROADCAST_PITCH = re.compile(
    r"notre stratégie|notre strategie",
    re.I,
)
_BROADCAST_RESULTS = re.compile(
    r"bons résultats|bons resultats|prêt à continuer|pret a continuer",
    re.I,
)
_CHANNEL_INVITE_BROADCAST = re.compile(
    r"50\s+spots|spots left|ready to join|private channel|join us|"
    r"strateg(y|ies)|rejoindre|channel where we post",
    re.I,
)
_BROADCAST_POSITIVE_REPLY = re.compile(
    r"\b("
    r"je suis intéressé|je suis interesse|je suis partant|"
    r"intéressé|interesse|pourquoi pas|"
    r"oui|ok|d'accord|daccord|vas[- ]?y|allons[- ]?y"
    r")\b",
    re.I,
)


def is_operator_ready_broadcast(text: str) -> bool:
    """Pager mass-mail: «Notre stratégie… Prêt à continuer?» (not bot scripts)."""
    t = (text or "").strip()
    if not t:
        return False
    if _READY_CONTINUE_BROADCAST.search(t):
        return True
    if _CHANNEL_INVITE_BROADCAST.search(t):
        return True
    return bool(_BROADCAST_PITCH.search(t) and _BROADCAST_RESULTS.search(t))


def is_operator_channel_invite_broadcast(text: str) -> bool:
    """English/FR follow-up: «50 spots left… Ready to join us?»"""
    t = (text or "").strip()
    return bool(t and _CHANNEL_INVITE_BROADCAST.search(t))


def is_broadcast_like_reply(
    text: str,
    attachments: list | None = None,
    *,
    message_reaction: str | None = None,
) -> bool:
    """Thumbs-up / like after operator broadcast — not a deposit screenshot."""
    if is_reaction_only_message(text, attachments, message_reaction=message_reaction):
        return True
    return is_short_affirmative(text) or is_positive_emoji_only(text)


def client_replied_to_operator_broadcast(
    messages: list[dict],
    last_in: dict,
    client_text: str,
    *,
    geo: str = "eg",
    message_reaction: str | None = None,
) -> bool:
    """Like / 👍 after Pager mass-mail or channel invite — continue funnel, not game ID."""
    if not is_broadcast_like_reply(
        client_text,
        last_in.get("attachments"),
        message_reaction=message_reaction,
    ):
        return False
    before_ts = str(last_in.get("createdAt") or "")
    last_op = _last_operator_body_before(messages, before_ts)
    if not last_op:
        return False
    if is_operator_ready_broadcast(last_op) or is_operator_channel_invite_broadcast(
        last_op
    ):
        return True
    return geo == "eg" and bool(
        re.search(r"spots|join|channel|strateg", last_op, re.I)
    )


def is_broadcast_positive_reply(text: str, *, geo: str = "cm") -> bool:
    """Short consent right after Pager mass-mail — not generic funnel interest."""
    t = (text or "").strip()
    if not t:
        return False
    if wants_registration_link(t) or is_post_link_registration_question(t):
        return False
    if is_deposit_tier_choice(t, geo=geo):
        return False
    if "?" in t and re.search(r"(?i)\blien\b", t):
        return False
    if len(t.split()) > 10 and not _BROADCAST_POSITIVE_REPLY.search(t):
        return False
    if is_short_affirmative(t):
        return True
    if _BROADCAST_POSITIVE_REPLY.search(t):
        if "?" in t and len(t.split()) > 2:
            if not re.search(
                r"(?i)\b(je\s+suis\s+intéress|je\s+suis\s+interesse|pr[eê]t)\b",
                t,
            ):
                return False
        return True
    return False


def outgoing_has_ready_continue_broadcast(
    outgoing_texts: list[str],
    *,
    limit: int = 16,
) -> bool:
    """Any operator broadcast pitch in recent outgoing texts."""
    checked = 0
    for raw in reversed(outgoing_texts or []):
        t = (raw or "").strip()
        if not t:
            continue
        if is_operator_ready_broadcast(t):
            return True
        checked += 1
        if checked >= limit:
            break
    return False


def _last_operator_body_before(messages: list[dict], before_ts: str) -> str:
    """Last non-system operator text strictly before client timestamp."""
    last_body = ""
    last_ts = ""
    for m in messages:
        direction = str(m.get("messageDirection") or "").strip().lower()
        if direction not in ("outgoing", "out"):
            continue
        if "oldStatusId" in m or "oldResponsibleId" in m:
            continue
        body = (m.get("text") or "").strip()
        if not body:
            continue
        ts = str(m.get("createdAt") or "")
        if not ts or not before_ts or ts > before_ts:
            continue
        if ts >= last_ts:
            last_ts = ts
            last_body = body
    return last_body


def client_replied_to_ready_broadcast(
    messages: list[dict],
    client_msg: dict,
    client_text: str,
    *,
    geo: str = "cm",
) -> bool:
    """True when client answered directly after operator mass-mail."""
    if is_deposit_tier_choice(client_text, geo=geo):
        return False
    if wants_registration_link(client_text):
        return False
    if not is_broadcast_positive_reply(client_text, geo=geo):
        return False
    client_ts = str(client_msg.get("createdAt") or "")
    if not client_ts:
        return False
    last_op = _last_operator_body_before(messages, client_ts)
    if not is_operator_ready_broadcast(last_op):
        return False
    return True


def is_affirmative_to_ready_broadcast(
    text: str,
    outgoing_texts: list[str],
    *,
    geo: str = "zm",
) -> bool:
    """Legacy helper — prefer client_replied_to_ready_broadcast with timestamps."""
    if not is_broadcast_positive_reply(text, geo=geo):
        return False
    return outgoing_has_ready_continue_broadcast(outgoing_texts)


def deposit_screenshot_nudge_reply(*, geo: str = "cm") -> str:
    from services.script_engine import load_script

    g = (geo or "cm").strip().lower()
    if g not in ("cm", "dj"):
        g = "cm"
    try:
        return load_script(g, "extras/deposit_screenshot_nudge")
    except FileNotFoundError:
        return (
            "Super, dès que vous aurez rechargé votre compte, "
            "envoyez-nous une capture d'écran."
        )


_DEPOSIT_CHECK_OUT = re.compile(
    r"\b("
    r"tu as fait le d[ée]p[oô]t|vous avez fait le d[ée]p[oô]t|"
    r"as[- ]tu fait le d[ée]p[oô]t|avez[- ]vous fait le d[ée]p[oô]t|"
    r"have you (made|done) (a |the |your )?deposit|did you deposit|"
    r"deposit\s*\?"
    r")\b",
    re.I,
)


def is_affirmative_to_deposit_check(
    text: str,
    outgoing_texts: list[str],
    *,
    geo: str = "zm",
) -> bool:
    """Short «Oui» after operator asked «Tu as fait le dépôt ?»."""
    if not is_short_affirmative(text):
        return False
    for raw in reversed(outgoing_texts or []):
        t = (raw or "").strip()
        if not t:
            continue
        if _DEPOSIT_CHECK_OUT.search(t):
            return True
    return False


def is_deposit_acknowledgment(text: str) -> bool:
    """«C'est bon» / payment SMS quote after client deposited."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(
        r"(c'?est bon|cest bon|c bon|cbien|c'?est fait|cest fait|"
        r"c'?est ok|cest ok|voil[àa]|tien|tiens|"
        r"paiement réussi|paiement reussi)\.?",
        t,
        re.I,
    ):
        return True
    return bool(
        re.search(
            r"\b(paiement|payment|recharg).{0,24}(réussi|reussi|successful|fait)\b",
            t,
            re.I,
        )
    )


def is_deposit_confirmation(text: str) -> bool:
    """Client says they deposited — never resend registration scripts."""
    t = (text or "").strip()
    if not t or is_deposit_question(t):
        return False
    if _AR_DEPOSIT.search(t):
        return True
    if re.search(
        r"\b(recharg|dépos|depos|depot|dépôt|paiement|payé|paye|verse)\b",
        t,
        re.I,
    ) and re.search(
        r"\b(fait|faite|termin|compte|avec|mon|le|la|j'ai|je)\b", t, re.I
    ):
        return True
    return bool(
        re.search(
            r"\b(done deposit|deposit done|deposited|made (a |my )?deposit|"
            r"i (have |'?ve )?deposited|sent deposit|finished deposit|"
            r"completed deposit|deposit complete|paid deposit|i paid)\b",
            t,
            re.I,
        )
    )


def is_registration_pending(text: str) -> bool:
    """Client has not registered yet — resend registration + link."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"(no|not)\s+yet\.?", t, re.I):
        return True
    if _AR_REG_PENDING.search(t):
        return True
    return bool(
        re.search(
            r"\b(not yet|no yet|haven'?t yet|havent yet|have not yet|"
            r"not registered|no registration|didn'?t register|"
            r"haven'?t registered|still working|will do|doing it|"
            r"not done|not finished|no i haven'?t|not for now|"
            r"give me time|need time|later today|maybe later|"
            r"still trying|working on it|in progress)\b",
            t,
            re.I,
        )
    )


def is_reg_confirmed_funnel_message(text: str, step: int) -> bool:
    """After reg link — client on site or confirmed; send 06_deposit."""
    if step < 4 or step >= 7:
        return False
    if is_registration_pending(text):
        return False
    return is_registration_confirmed(text)


def _classify_arabic(t: str) -> Intent | None:
    if not t or not _ARABIC.search(t):
        return None
    if _GAME_ID_EG.search(t):
        return Intent.GAME_ID_TEXT
    if _AR_COMPLAINT.search(t):
        return Intent.COMPLAINT
    if is_deposit_question(t):
        return Intent.QUESTION
    if _AR_DEPOSIT.search(t):
        return Intent.DEPOSIT_DONE
    if is_registration_confirmed(t):
        return Intent.POSITIVE
    if is_deferral_reply(t):
        return Intent.UNKNOWN
    if is_app_or_browser_question(t):
        return Intent.QUESTION
    if wants_details_after_intro(t):
        return Intent.INTERESTED
    if _AR_JOIN_DETAILS.search(t):
        return Intent.INTERESTED
    if _AR_INTERESTED.search(t) or _AR_DETAILS.search(t):
        return Intent.INTERESTED
    if re.fullmatch(r"تم\.?", t.strip()) or t.strip() in (
        "تم",
        "تمام",
        "ماشي",
        "معك",
        "موافق",
        "ايو",
        "ايوه",
        "آه",
        "اه",
        "نعم",
        "نجرب",
    ):
        return Intent.POSITIVE
    if "من مصر" in t or "مصري" in t:
        return Intent.INTERESTED
    if _AR_READY.search(t):
        return Intent.READY
    if _AR_POSITIVE.search(t):
        return Intent.POSITIVE
    if _AR_GREETING.search(t) and len(t.split()) <= 8:
        return Intent.INTERESTED
    if "؟" in t or "?" in t:
        return Intent.QUESTION
    return None


def _classify_french(t: str) -> Intent | None:
    if is_refusal_reply(t):
        return Intent.DECLINED
    if is_age_answer(t):
        return Intent.POSITIVE
    if _FR_LINK_ASK.search(t):
        return Intent.READY
    if _FR_REG.search(t) and ("?" in t or "comment" in t.lower()):
        return Intent.INTERESTED
    if is_post_link_registration_question(t):
        return Intent.QUESTION
    if _FR_READY.search(t):
        return Intent.READY
    if _FR_INTERESTED.search(t):
        return Intent.INTERESTED
    if is_short_affirmative(t):
        return Intent.POSITIVE
    if re.fullmatch(r"oui\.?", t.strip(), re.I) or t.strip().lower() in (
        "oui",
        "ouais",
        "d'accord",
        "daccord",
        "ok",
        "okay",
    ):
        return Intent.POSITIVE
    if _FR_POSITIVE.search(t):
        return Intent.POSITIVE
    if _FR_GREETING.search(t) and len(t.split()) <= 8:
        return Intent.INTERESTED
    if "?" in t:
        return Intent.QUESTION
    return None


def classify(
    text: str,
    *,
    has_image: bool = False,
    has_ad: bool = False,
    geo: str = "zm",
    attachments: list | None = None,
    funnel_step: int = 0,
    message_reaction: str | None = None,
) -> Intent:
    t = (text or "").strip()
    if is_funnel_earning_interest(t):
        return Intent.INTERESTED
    if is_phone_number_request(t):
        return Intent.PHONE_REQUEST
    if is_money_request(t):
        return Intent.MONEY_REQUEST
    if is_refusal_reply(t):
        return Intent.DECLINED
    if is_funnel_positive_reaction(
        t,
        attachments,
        funnel_step=funnel_step,
        geo=geo,
        message_reaction=message_reaction,
    ):
        return Intent.POSITIVE
    if contains_positive_emoji(t) or is_positive_emoji_only(t):
        return Intent.POSITIVE
    if not t and is_messenger_reaction_attachment(attachments or []):
        return Intent.POSITIVE
    if not t and is_positive_message_reaction(message_reaction):
        return Intent.POSITIVE
    if has_ad and not t and not has_image:
        return Intent.INTERESTED
    if has_image and not t:
        if is_messenger_reaction_attachment(attachments or []):
            return Intent.POSITIVE
        if is_positive_message_reaction(message_reaction):
            return Intent.POSITIVE
        if funnel_step < 4:
            return Intent.POSITIVE
        return Intent.IMAGE_ONLY
    game_re = _GAME_ID
    if game_re.search(t):
        return Intent.GAME_ID_TEXT
    if geo != "eg" and _GAME_ID_LEGACY.search(t):
        return Intent.GAME_ID_TEXT
    if geo == "eg" or _ARABIC.search(t):
        ar = _classify_arabic(t)
        if ar is not None:
            return ar
    if geo == "dj" or geo == "cm" or _FRENCH_LATIN.search(t):
        fr = _classify_french(t)
        if fr is not None:
            return fr
    if _COMPLAINT.search(t):
        return Intent.COMPLAINT
    if is_deposit_confirmation(t):
        return Intent.DEPOSIT_DONE
    if is_deposit_acknowledgment(t):
        return Intent.DEPOSIT_DONE
    if is_registration_confirmed(t):
        return Intent.POSITIVE
    if is_deferral_reply(t):
        return Intent.UNKNOWN
    if is_deposit_tier_choice(t, geo=geo):
        return Intent.READY
    if _ACK.search(t):
        return Intent.POSITIVE
    if _JOINED.search(t):
        return Intent.JOINED
    if _READY.search(t):
        return Intent.READY
    if _INTERESTED.search(t):
        return Intent.INTERESTED
    if _GREETING.search(t) and len(t.split()) <= 6:
        return Intent.INTERESTED
    if re.fullmatch(r"how\??", t.strip(), re.I):
        return Intent.QUESTION
    if _POSITIVE.search(t):
        return Intent.POSITIVE
    if "?" in t or re.search(r"\b(how|what|when|why|can you)\b", t, re.I):
        return Intent.QUESTION
    if t:
        return Intent.UNKNOWN
    return Intent.UNKNOWN


def needs_human(intent: Intent, step: int, *, no_status: bool = False) -> bool:
    if intent == Intent.COMPLAINT:
        return True
    # Funnel steps 1–3 — try scripts, never escalate on unknown/no.
    if step < 4 and intent in (Intent.UNKNOWN, Intent.QUESTION):
        return False
    if step >= 5 and intent in (Intent.UNKNOWN, Intent.QUESTION):
        return False
    if intent in (Intent.QUESTION, Intent.UNKNOWN):
        return True
    return False


def needs_human_for_text(
    intent: Intent, step: int, text: str, *, no_status: bool = False, geo: str = "zm"
) -> bool:
    if intent in (Intent.MONEY_REQUEST, Intent.PHONE_REQUEST, Intent.DECLINED):
        return False
    if geo == "eg" and step < 6 and intent in (Intent.UNKNOWN, Intent.QUESTION):
        return False
    if (
        geo in ("zm", "dj", "cm")
        and no_status
        and step < 6
        and intent in (Intent.UNKNOWN, Intent.QUESTION)
    ):
        return False
    if is_post_link_registration_question(text) and step < 7:
        return False
    if is_xbet_site_question(text) and step < 8:
        return False
    if is_deferral_reply(text) and step < 6:
        return False
    if is_reg_confirmed_funnel_message(text, step):
        return False
    if is_registration_pending(text) and step < 6:
        return False
    if is_ready_for_registration(text, geo=geo) and step < 5:
        return False
    if is_deposit_tier_choice(text, geo=geo) and step < 5:
        return False
    return needs_human(intent, step, no_status=no_status)
