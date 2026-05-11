"""
notifier.py — Envoi de notifications SMS.

En mode stub (SMS_STUB=true), les messages sont loggés dans la console.
Pour activer Twilio : mettre SMS_STUB=false et remplir les variables Twilio dans .env
"""

import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def send_sms(to_number: str, message: str) -> bool:
    """
    Envoie un SMS au numéro donné.
    Retourne True si succès, False sinon.
    """
    stub_mode = os.getenv("SMS_STUB", "true").lower() == "true"

    if stub_mode:
        _send_stub(to_number, message)
        return True

    return _send_twilio(to_number, message)


def _send_stub(to_number: str, message: str):
    """Mode développement : affiche le SMS dans les logs."""
    separator = "=" * 60
    logger.info(separator)
    logger.info("📱 SMS (STUB MODE — non envoyé)")
    logger.info(f"   À       : {to_number}")
    logger.info(f"   Heure   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"   Message : {message}")
    logger.info(separator)


def _send_twilio(to_number: str, message: str) -> bool:
    """Envoi réel via Twilio — activer en remplissant .env"""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")

    if not all([account_sid, auth_token, from_number]):
        logger.error(
            "Twilio non configuré. Vérifier TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN et TWILIO_FROM_NUMBER dans .env"
        )
        return False

    try:
        from twilio.rest import Client  # pip install twilio
        client = Client(account_sid, auth_token)
        msg = client.messages.create(
            body=message,
            from_=from_number,
            to=to_number,
        )
        logger.info(f"SMS envoyé via Twilio — SID: {msg.sid}")
        return True
    except ImportError:
        logger.error("Package 'twilio' non installé. Lancer: pip install twilio")
        return False
    except Exception as e:
        logger.error(f"Erreur Twilio: {e}")
        return False


def format_alert_message(
    terrain_nom: str,
    date: str,
    heure: str,
    nb_joueurs: int,
    url: str,
) -> str:
    """Formate le message SMS pour une alerte déclenchée."""
    return (
        f"⛳ Départ disponible!\n"
        f"{terrain_nom}\n"
        f"📅 {date} à {heure}\n"
        f"👥 {nb_joueurs} joueur(s)\n"
        f"🔗 {url}"
    )
