"""Localization pack (§20).

Six launch locales: en-US, en-GB, es, fr, de, pt-BR.

The strings live in plain dicts keyed by (locale, key). The lookup is
locale-falls-back-to-base-language-falls-back-to-en-US so a missing
pt-BR key falls back to pt, then to en-US.

This module is *additive*: existing code paths that use hard-coded
English copy still work. Newer call sites can pass `locale=...` to
notifications.py / signup.py to render the localized string.
"""

from __future__ import annotations

from typing import Dict, Optional


# Canonical locale set per spec §20.
LOCALES = ("en-US", "en-GB", "es", "fr", "de", "pt-BR")
DEFAULT_LOCALE = "en-US"


def _normalize(loc: Optional[str]) -> str:
    if not loc:
        return DEFAULT_LOCALE
    loc = loc.strip()
    # Direct match
    if loc in LOCALES:
        return loc
    # Case-normalized match
    for cand in LOCALES:
        if cand.lower() == loc.lower():
            return cand
    # Region-stripped fallback (e.g. "en-AU" -> "en-US")
    base = loc.split("-")[0].lower()
    for cand in LOCALES:
        if cand.lower().startswith(base + "-") or cand.lower() == base:
            return cand
    return DEFAULT_LOCALE


# ──────────────────────────────────────────────────────────────────────────
# String catalog. Keys are short, stable, and namespaced. Adding a new
# key only requires the en-US row — the lookup falls back so partial
# coverage is acceptable.
# ──────────────────────────────────────────────────────────────────────────
STRINGS: Dict[str, Dict[str, str]] = {

    # ── Signup error messages (§15) ─────────────────────────────────────
    "err.email_already_exists": {
        "en-US": "An account already exists for this email address.",
        "en-GB": "An account already exists for this email address.",
        "es":    "Ya existe una cuenta con esta dirección de correo.",
        "fr":    "Un compte existe déjà pour cette adresse e-mail.",
        "de":    "Für diese E-Mail-Adresse besteht bereits ein Konto.",
        "pt-BR": "Já existe uma conta com este endereço de e-mail.",
    },
    "err.disposable_email_blocked": {
        "en-US": "Disposable email addresses aren't allowed. Use a permanent inbox.",
        "en-GB": "Disposable email addresses aren't allowed. Use a permanent inbox.",
        "es":    "No se permiten correos desechables. Use una bandeja permanente.",
        "fr":    "Les adresses e-mail jetables ne sont pas autorisées.",
        "de":    "Wegwerf-E-Mail-Adressen sind nicht zulässig.",
        "pt-BR": "Endereços de e-mail descartáveis não são permitidos.",
    },
    "err.rate_limited": {
        "en-US": "Too many attempts. Please wait a moment and try again.",
        "en-GB": "Too many attempts. Please wait a moment and try again.",
        "es":    "Demasiados intentos. Espere un momento e inténtelo de nuevo.",
        "fr":    "Trop de tentatives. Veuillez patienter avant de réessayer.",
        "de":    "Zu viele Versuche. Bitte einen Moment warten und erneut versuchen.",
        "pt-BR": "Muitas tentativas. Aguarde um momento e tente novamente.",
    },
    "err.country_restricted": {
        "en-US": "We can't accept signups from this jurisdiction yet.",
        "en-GB": "We can't accept signups from this jurisdiction yet.",
        "es":    "Aún no aceptamos registros desde esta jurisdicción.",
        "fr":    "Nous n'acceptons pas encore d'inscriptions depuis cette juridiction.",
        "de":    "Anmeldungen aus dieser Region sind noch nicht möglich.",
        "pt-BR": "Ainda não aceitamos inscrições desta jurisdição.",
    },
    "err.manual_review_required": {
        "en-US": "Your account needs a quick review. We'll email you shortly.",
        "en-GB": "Your account needs a quick review. We'll email you shortly.",
        "es":    "Su cuenta requiere una revisión rápida. Le escribiremos pronto.",
        "fr":    "Votre compte nécessite une vérification rapide. Nous vous écrirons sous peu.",
        "de":    "Ihr Konto wird kurz geprüft. Wir melden uns per E-Mail.",
        "pt-BR": "Sua conta precisa de uma revisão rápida. Entraremos em contato em breve.",
    },
    "err.payment_declined": {
        "en-US": "Your payment was declined. Please try a different card.",
        "en-GB": "Your payment was declined. Please try a different card.",
        "es":    "Su pago fue rechazado. Intente con otra tarjeta.",
        "fr":    "Votre paiement a été refusé. Essayez une autre carte.",
        "de":    "Ihre Zahlung wurde abgelehnt. Versuchen Sie eine andere Karte.",
        "pt-BR": "Seu pagamento foi recusado. Tente outro cartão.",
    },

    # ── Email subjects + headings ───────────────────────────────────────
    "email.welcome.subject": {
        "en-US": "Welcome to Temveris — verify your email",
        "en-GB": "Welcome to Temveris — verify your email",
        "es":    "Bienvenido a Temveris — verifique su correo",
        "fr":    "Bienvenue sur Temveris — vérifiez votre e-mail",
        "de":    "Willkommen bei Temveris — E-Mail bestätigen",
        "pt-BR": "Bem-vindo ao Temveris — verifique seu e-mail",
    },
    "email.welcome.heading": {
        "en-US": "Welcome to Temveris, {first_name}.",
        "en-GB": "Welcome to Temveris, {first_name}.",
        "es":    "Bienvenido a Temveris, {first_name}.",
        "fr":    "Bienvenue sur Temveris, {first_name}.",
        "de":    "Willkommen bei Temveris, {first_name}.",
        "pt-BR": "Bem-vindo ao Temveris, {first_name}.",
    },
    "email.welcome.cta_verify": {
        "en-US": "Verify Email",
        "en-GB": "Verify Email",
        "es":    "Verificar correo",
        "fr":    "Vérifier l'e-mail",
        "de":    "E-Mail bestätigen",
        "pt-BR": "Verificar e-mail",
    },
    "email.welcome.cta_open": {
        "en-US": "Open Temveris",
        "en-GB": "Open Temveris",
        "es":    "Abrir Temveris",
        "fr":    "Ouvrir Temveris",
        "de":    "Temveris öffnen",
        "pt-BR": "Abrir Temveris",
    },
    "email.verify.subject": {
        "en-US": "Verify your Temveris email",
        "en-GB": "Verify your Temveris email",
        "es":    "Verifique su correo de Temveris",
        "fr":    "Vérifiez votre e-mail Temveris",
        "de":    "Temveris-E-Mail bestätigen",
        "pt-BR": "Verifique seu e-mail do Temveris",
    },

    # ── Trial lifecycle subjects (Day 0/1/7/14/18/25/30/32/35) ─────────
    "email.trial.day_0.subject": {
        "en-US": "Your Temveris trial has started",
        "es":    "Su prueba de Temveris ha comenzado",
        "fr":    "Votre essai Temveris a commencé",
        "de":    "Ihre Temveris-Testphase hat begonnen",
        "pt-BR": "Seu teste do Temveris começou",
    },
    "email.trial.day_1.subject": {
        "en-US": "Set up your first workforce policy",
        "es":    "Configure su primera política laboral",
        "fr":    "Configurez votre première politique RH",
        "de":    "Erste Arbeitsrichtlinie konfigurieren",
        "pt-BR": "Configure sua primeira política de equipe",
    },
    "email.trial.day_7.subject": {
        "en-US": "How's your first week going?",
        "es":    "¿Cómo va su primera semana?",
        "fr":    "Comment se passe votre première semaine ?",
        "de":    "Wie läuft Ihre erste Woche?",
        "pt-BR": "Como está sua primeira semana?",
    },
    "email.trial.day_14.subject": {
        "en-US": "Your trial is halfway through",
        "es":    "Va por la mitad de su prueba",
        "fr":    "Vous êtes à mi-parcours de votre essai",
        "de":    "Halbzeit Ihrer Testphase",
        "pt-BR": "Você está na metade do teste",
    },
    "email.trial.day_18.subject": {
        "en-US": "Convert your Temveris trial",
        "es":    "Convierta su prueba a un plan de pago",
        "fr":    "Passez à un abonnement payant",
        "de":    "Testphase in ein Abo umwandeln",
        "pt-BR": "Converta seu teste em assinatura",
    },
    "email.trial.day_25.subject": {
        "en-US": "5 days left in your Temveris trial",
        "es":    "Quedan 5 días de su prueba Temveris",
        "fr":    "5 jours restants pour votre essai Temveris",
        "de":    "Noch 5 Tage in Ihrer Temveris-Testphase",
        "pt-BR": "Faltam 5 dias no seu teste Temveris",
    },
    "email.trial.day_30.subject": {
        "en-US": "Your Temveris trial has ended",
        "es":    "Su prueba de Temveris ha terminado",
        "fr":    "Votre essai Temveris est terminé",
        "de":    "Ihre Temveris-Testphase ist beendet",
        "pt-BR": "Seu teste do Temveris terminou",
    },
    "email.trial.day_32.subject": {
        "en-US": "Last chance to reactivate Temveris",
        "es":    "Última oportunidad de reactivar Temveris",
        "fr":    "Dernière chance pour réactiver Temveris",
        "de":    "Letzte Chance, Temveris zu reaktivieren",
        "pt-BR": "Última chance para reativar o Temveris",
    },
    "email.trial.day_35.subject": {
        "en-US": "Temveris workspace closure notice",
        "es":    "Aviso de cierre de espacio de trabajo Temveris",
        "fr":    "Avis de fermeture de l'espace de travail Temveris",
        "de":    "Mitteilung zur Schließung des Temveris-Arbeitsbereichs",
        "pt-BR": "Aviso de encerramento do espaço de trabalho Temveris",
    },

    # ── Dunning subjects ────────────────────────────────────────────────
    "email.dunning.day_1.subject": {
        "en-US": "We couldn't process your Temveris payment",
        "es":    "No pudimos procesar su pago de Temveris",
        "fr":    "Nous n'avons pas pu traiter votre paiement Temveris",
        "de":    "Ihre Temveris-Zahlung konnte nicht verarbeitet werden",
        "pt-BR": "Não conseguimos processar seu pagamento do Temveris",
    },
    "email.dunning.day_3.subject": {
        "en-US": "Action needed — Temveris payment still failing",
        "es":    "Acción necesaria — pago de Temveris aún falla",
        "fr":    "Action requise — paiement Temveris toujours en échec",
        "de":    "Aktion erforderlich — Temveris-Zahlung weiterhin fehlerhaft",
        "pt-BR": "Ação necessária — pagamento do Temveris ainda falhando",
    },
    "email.dunning.day_7.subject": {
        "en-US": "Final reminder — your Temveris workspace will be restricted",
        "es":    "Recordatorio final — su Temveris quedará restringido",
        "fr":    "Dernier rappel — votre espace Temveris sera restreint",
        "de":    "Letzte Erinnerung — Ihr Temveris-Bereich wird eingeschränkt",
        "pt-BR": "Lembrete final — seu Temveris será restrito",
    },
    "email.dunning.day_14.subject": {
        "en-US": "Your Temveris workspace has been restricted",
        "es":    "Su espacio Temveris ha sido restringido",
        "fr":    "Votre espace Temveris est restreint",
        "de":    "Ihr Temveris-Arbeitsbereich ist eingeschränkt",
        "pt-BR": "Seu espaço Temveris foi restrito",
    },

    # ── Onboarding labels ───────────────────────────────────────────────
    "onboarding.confirm_email":      {"en-US": "Confirm email",          "es": "Confirme su correo",        "fr": "Confirmer l'e-mail",      "de": "E-Mail bestätigen",   "pt-BR": "Confirmar e-mail"},
    "onboarding.first_policy":       {"en-US": "Configure first policy", "es": "Configure su primera política","fr": "Configurer la première politique", "de": "Erste Richtlinie konfigurieren", "pt-BR": "Configure a primeira política"},
    "onboarding.invite_teammate":    {"en-US": "Invite first teammate",  "es": "Invite a un compañero",     "fr": "Invitez votre premier coéquipier", "de": "Erstes Teammitglied einladen",   "pt-BR": "Convide o primeiro colega"},
    "onboarding.review_dashboard":   {"en-US": "Review dashboard",       "es": "Revise el panel",           "fr": "Consulter le tableau de bord",     "de": "Dashboard prüfen",               "pt-BR": "Revisar painel"},
    "onboarding.install_tools":      {"en-US": "Install required tools", "es": "Instale las herramientas",  "fr": "Installer les outils requis",      "de": "Erforderliche Tools installieren", "pt-BR": "Instalar ferramentas"},
    "onboarding.review_billing":     {"en-US": "Review billing / trial", "es": "Revise facturación / prueba","fr": "Vérifier facturation / essai",     "de": "Abrechnung / Testphase prüfen",  "pt-BR": "Revisar cobrança / teste"},

    # ── Marketing consent (GDPR / UK GDPR opt-in copy) ──────────────────
    "consent.marketing.label": {
        "en-US": "Send me product updates and tips. I can unsubscribe at any time.",
        "en-GB": "Send me product updates and tips. I can unsubscribe at any time.",
        "es":    "Envíenme actualizaciones de producto y consejos. Puedo darme de baja en cualquier momento.",
        "fr":    "Envoyez-moi des nouveautés produit et des conseils. Je peux me désabonner à tout moment.",
        "de":    "Senden Sie mir Produktneuigkeiten und Tipps. Ich kann mich jederzeit abmelden.",
        "pt-BR": "Envie-me novidades e dicas. Posso cancelar a inscrição a qualquer momento.",
    },
}


def t(key: str, locale: Optional[str] = None, **kwargs) -> str:
    """Look up `key` in the active locale with fallback chain
    locale → base-language → en-US. Supports {placeholder} formatting
    via kwargs.

    Unknown keys are returned verbatim (returned to caller as-is) so
    missing translations show up obviously in QA without crashing
    production.
    """
    loc = _normalize(locale)
    row = STRINGS.get(key)
    if row is None:
        return key  # surface the missing key — easier to spot in QA than silent fallback

    if loc in row:
        return row[loc].format(**kwargs) if kwargs else row[loc]

    # Base-language fallback (e.g. en-GB → en-US, pt-BR → pt → en-US).
    base = loc.split("-")[0]
    for cand_loc, val in row.items():
        if cand_loc.startswith(base + "-") or cand_loc == base:
            return val.format(**kwargs) if kwargs else val

    # Final fallback: en-US.
    val = row.get(DEFAULT_LOCALE, key)
    return val.format(**kwargs) if kwargs else val


def supported_locales() -> tuple:
    return LOCALES
