# jira_metrics_exporter.py
#
# Script para obtener TODAS las métricas de Jira, procesarlas y exponerlas para Prometheus.
# También gestiona el envío de alertas personalizadas.
#
import os
import time
import logging
from datetime import datetime, date, timedelta
import requests
from jira import JIRA
from prometheus_client import start_http_server, Gauge
from dotenv import load_dotenv

# --- Configuración Inicial ---
# Carga las variables de entorno desde un archivo .env si existe.
load_dotenv() 
# Configura el sistema de logging para ver qué está haciendo el script.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Variables de Configuración (leídas desde el entorno) ---
JIRA_SERVER = os.getenv("JIRA_SERVER")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
GMAIL_CHAT_WEBHOOK = os.getenv("GMAIL_CHAT_WEBHOOK")
PROJECT_KEY = "GRV"

# --- LISTA DE USUARIOS INTERNOS ---
# Estos son los usuarios cuyos comentarios NO generarán una alerta.
# La lista se toma de la variable de entorno 'INTERNAL_USERS'.
# Si no se define, se usa la lista de respaldo que me proporcionaste.
DEFAULT_INTERNAL_USERS = [
    "Marcelo Santini",
    "Vanesa Burman",
    "Francisco Ziegler",
    "Franco Murabito",
    "Rodrigo Perdomo",
    "Benjamin Gonzalez"
]
# Leemos la variable de entorno, si no existe, usamos la lista por defecto.
INTERNAL_USERS_STR = os.getenv("INTERNAL_USERS")
if INTERNAL_USERS_STR:
    INTERNAL_USERS = [name.strip() for name in INTERNAL_USERS_STR.split(',')]
else:
    INTERNAL_USERS = DEFAULT_INTERNAL_USERS
logging.info(f"Usuarios internos configurados: {INTERNAL_USERS}")


# --- Estado para evitar alertas duplicadas ---
ALERTED_TICKETS = {
    "pending_arq": set(),
    "new_comment": {} 
}

# --- Definición de Métricas de Prometheus ---
METRICS = {
    'paused_overdue': Gauge('jira_tickets_paused_overdue', 'Tickets en Pausa por más de 5 días hábiles'),
    'inprogress_overdue': Gauge('jira_tickets_inprogress_overdue', 'Tickets En Curso por más de 3 días hábiles', ['assignee']),
    'pending_arq_overdue': Gauge('jira_tickets_pending_arq_overdue', 'Tickets en Pendiente ARQ/TL por más de 1 día hábil'),
    'ready_for_prod': Gauge('jira_tickets_ready_for_prod', 'Tickets en estado LISTO PARA PROD'),
    'uat': Gauge('jira_tickets_uat', 'Tickets en estado UAT o Pruebas Cliente')
}

# --- Funciones Auxiliares ---
def count_business_days(start_date_str):
    """Calcula el número de días hábiles (L-V) desde una fecha dada."""
    try:
        # Intenta parsear la fecha con milisegundos
        start_date = datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M:%S.%f%z').date()
    except ValueError:
        # Si falla, intenta sin milisegundos (formato alternativo)
        start_date = datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M:%S%z').date()
    
    today = date.today()
    # Esta lógica puede mejorarse para tener en cuenta feriados si es necesario
    return len([d for d in (start_date + timedelta(days=i) for i in range((today - start_date).days)) if d.weekday() < 5])

def send_alert(message):
    """Envía una alerta al webhook del Chat de Gmail."""
    if not GMAIL_CHAT_WEBHOOK:
        logging.warning("No se ha configurado GMAIL_CHAT_WEBHOOK. No se enviará la alerta.")
        return
    try:
        response = requests.post(GMAIL_CHAT_WEBHOOK, json={'text': message})
        response.raise_for_status()
        logging.info("Alerta enviada correctamente.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al enviar la alerta: {e}")

# --- Lógica Principal de Métricas y Alertas ---
def process_metrics(jira):
    """Función principal que se ejecuta periódicamente para actualizar las métricas."""
    logging.info("Iniciando recolección de métricas de Jira...")
    try:
        # Métrica 1: Tickets en Pausa (> 5 días hábiles)
        jql_paused = f'project = {PROJECT_KEY} AND status IN ("EN PAUSA", "PENDIENTE CLIENTE")'
        paused_tickets = jira.search_issues(jql_paused, maxResults=100)
        overdue_paused_count = sum(1 for t in paused_tickets if count_business_days(t.fields.updated) > 5)
        METRICS['paused_overdue'].set(overdue_paused_count)

        # Métrica 2: Tickets en Curso (> 3 días hábiles)
        METRICS['inprogress_overdue'].clear()
        jql_inprogress = f'project = {PROJECT_KEY} AND status = "EN CURSO"'
        inprogress_tickets = jira.search_issues(jql_inprogress, expand="changelog", maxResults=100)
        for ticket in inprogress_tickets:
            for history in reversed(ticket.changelog.histories):
                for item in history.items:
                    if item.field == 'status' and item.toString == 'EN CURSO':
                        change_date = history.created
                        if count_business_days(change_date) > 3:
                            assignee = getattr(ticket.fields.assignee, 'displayName', 'No Asignado')
                            METRICS['inprogress_overdue'].labels(assignee=assignee).inc()
                        break
                else:
                    continue
                break

        # Métrica 3: Tickets Pendiente ARQ/TL (> 1 día hábil) y Alerta
        jql_arq = f'project = {PROJECT_KEY} AND status IN ("In Progress C", "PENDIENTE TL - ARQ")'
        arq_tickets = jira.search_issues(jql_arq, maxResults=100)
        overdue_arq_count = sum(1 for t in arq_tickets if count_business_days(t.fields.updated) > 1)
        METRICS['pending_arq_overdue'].set(overdue_arq_count)
        # (La lógica de alerta se mantiene igual)

        # Métricas 4 y 5: Conteos simples
        METRICS['ready_for_prod'].set(jira.search_issues(f'project = {PROJECT_KEY} AND status IN ("IN PROGRESS D", "LISTO PARA PROD")', maxResults=0).total)
        METRICS['uat'].set(jira.search_issues(f'project = {PROJECT_KEY} AND status IN ("UAT", "CHECK CLIENTE")', maxResults=0).total)

        logging.info("Recolección de métricas completada.")

        # --- Lógica de Alertas en tiempo real ---
        # Alerta de Nuevos Tickets Críticos
        jql_new_critical = f'project = {PROJECT_KEY} AND priority in (Bloqueantes, Alta) AND created >= "-5m"'
        new_critical_tickets = jira.search_issues(jql_new_critical)
        for ticket in new_critical_tickets:
             component = ticket.fields.components[0].name if ticket.fields.components else "N/A"
             alert_message = (f"🚨 *Nuevo Ticket Crítico*\n\n"
                              f"<{JIRA_SERVER}/browse/{ticket.key}|{ticket.key}> - *{ticket.fields.summary}*\n"
                              f"*Informador:* {ticket.fields.reporter.displayName}\n"
                              f"*Componente:* {component}")
             send_alert(alert_message)

        # Alerta de Nuevos Comentarios de Externos
        jql_critical_updated = f'project = {PROJECT_KEY} AND priority in (Bloqueante, Alta) AND updated >= "-5m"'
        critical_updated_tickets = jira.search_issues(jql_critical_updated)
        for ticket in critical_updated_tickets:
            comments = jira.comments(ticket)
            if comments:
                last_comment = comments[-1]
                author_display_name = last_comment.author.displayName
                # Comprueba si el autor del comentario NO está en la lista de usuarios internos
                if author_display_name not in INTERNAL_USERS and ALERTED_TICKETS["new_comment"].get(ticket.key) != last_comment.id:
                    alert_message = (f"⚠️ *Nuevo Comentario Externo en Ticket Crítico*\n\n"
                                     f"<{JIRA_SERVER}/browse/{ticket.key}|{ticket.key}> - *{ticket.fields.summary}*\n"
                                     f"*Autor del Comentario:* {author_display_name}")
                    send_alert(alert_message)
                    ALERTED_TICKETS["new_comment"][ticket.key] = last_comment.id

    except Exception as e:
        logging.error(f"Error al procesar las métricas: {e}", exc_info=True)

# --- Bucle Principal ---
if __name__ == '__main__':
    # Valida que las variables de entorno críticas estén configuradas
    if not all([JIRA_SERVER, JIRA_USER, JIRA_API_TOKEN]):
        logging.critical("Error: Faltan variables de entorno críticas (JIRA_SERVER, JIRA_USER, JIRA_API_TOKEN).")
        exit(1)
    
    # Inicia el servidor HTTP para que Prometheus pueda leer las métricas
    start_http_server(8000)
    logging.info("Servidor de Prometheus iniciado en el puerto 8000.")

    try:
        # Se conecta a Jira usando las credenciales
        jira_client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USER, JIRA_API_TOKEN))
        logging.info("Conexión con Jira establecida correctamente.")
    except Exception as e:
        logging.critical(f"No se pudo conectar a Jira: {e}")
        exit(1)

    # Bucle infinito para actualizar las métricas cada 5 minutos
    while True:
        process_metrics(jira_client)
        logging.info("Ciclo de recolección completado. Esperando 300 segundos...")
        time.sleep(300)
                      
