# jira_metrics_exporter.py
#
# Script HÍBRIDO:
# 1. En un hilo de fondo: Obtiene métricas de Jira y las envía (push) a Grafana Cloud.
# 2. En el hilo principal: Inicia un servidor web Flask para satisfacer los health checks de Render.
#
import os
import time
import logging
import threading # Para ejecutar tareas en segundo plano
import functools # <--- NUEVO: Necesario para la autenticación corregida
from datetime import datetime, date, timedelta
import requests
from jira import JIRA
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
from prometheus_client.exposition import basic_auth_handler
from dotenv import load_dotenv
from flask import Flask # El mini servidor web

# --- Configuración Inicial ---
load_dotenv() 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Variables de Configuración (leídas desde el entorno) ---
JIRA_SERVER = os.getenv("JIRA_SERVER")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
PROJECT_KEY = "GRV"
GMAIL_CHAT_WEBHOOK = os.getenv("GMAIL_CHAT_WEBHOOK")
GRAFANA_CLOUD_URL = os.getenv('GRAFANA_CLOUD_URL')
GRAFANA_CLOUD_INSTANCE_ID = os.getenv('GRAFANA_CLOUD_INSTANCE_ID')
GRAFANA_CLOUD_API_KEY = os.getenv('GRAFANA_CLOUD_API_KEY')

# --- Lista de Usuarios Internos ---
DEFAULT_INTERNAL_USERS = [
    "Marcelo Santini", "Vanesa Burman", "Francisco Ziegler",
    "Franco Murabito", "Rodrigo Perdomo", "Benjamin Gonzalez"
]
INTERNAL_USERS_STR = os.getenv("INTERNAL_USERS")
INTERNAL_USERS = [name.strip() for name in INTERNAL_USERS_STR.split(',')] if INTERNAL_USERS_STR else DEFAULT_INTERNAL_USERS
logging.info(f"Usuarios internos configurados: {INTERNAL_USERS}")

# --- Estado para evitar alertas duplicadas ---
ALERTED_TICKETS = {"new_comment": {}}

# --- Funciones Auxiliares (sin cambios) ---
def count_business_days(start_date_str):
    """Calcula el número de días hábiles (L-V) desde una fecha dada."""
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M:%S.%f%z').date()
    except ValueError:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M:%S%z').date()
    today = date.today()
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

# --- Lógica de Métricas (ahora dentro de una función para el hilo de fondo) ---
def metrics_and_alerts_loop():
    """Bucle infinito que se ejecuta en segundo plano para recolectar y enviar métricas."""
    logging.info("Iniciando hilo de recolección de métricas...")
    
    if not all([JIRA_SERVER, JIRA_USER, JIRA_API_TOKEN, GRAFANA_CLOUD_URL, GRAFANA_CLOUD_INSTANCE_ID, GRAFANA_CLOUD_API_KEY]):
        logging.critical("Error en hilo de fondo: Faltan variables de entorno críticas. El hilo se detendrá.")
        return

    try:
        jira_client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USER, JIRA_API_TOKEN))
        logging.info("Conexión con Jira establecida en hilo de fondo.")
    except Exception as e:
        logging.critical(f"No se pudo conectar a Jira en hilo de fondo: {e}. El hilo se detendrá.")
        return

    while True:
        logging.info("Iniciando ciclo de recolección...")
        registry = CollectorRegistry()
        METRICS = {
            'paused_overdue': Gauge('jira_tickets_paused_overdue', 'Tickets en Pausa por más de 5 días hábiles', registry=registry),
            'inprogress_overdue': Gauge('jira_tickets_inprogress_overdue', 'Tickets En Curso por más de 3 días hábiles', ['assignee'], registry=registry),
            'pending_arq_overdue': Gauge('jira_tickets_pending_arq_overdue', 'Tickets en Pendiente ARQ/TL por más de 1 día hábil', registry=registry),
            'ready_for_prod': Gauge('jira_tickets_ready_for_prod', 'Tickets en estado LISTO PARA PROD', registry=registry),
            'uat': Gauge('jira_tickets_uat', 'Tickets en estado UAT o Pruebas Cliente', registry=registry)
        }
        
        try:
            jql_paused = f'project = {PROJECT_KEY} AND status = "EN PAUSA"'
            paused_tickets = jira_client.search_issues(jql_paused, maxResults=100)
            METRICS['paused_overdue'].set(sum(1 for t in paused_tickets if count_business_days(t.fields.updated) > 5))
            
            METRICS['inprogress_overdue'].clear()
            jql_inprogress = f'project = {PROJECT_KEY} AND status = "EN CURSO"'
            inprogress_tickets = jira_client.search_issues(jql_inprogress, expand="changelog", maxResults=100)
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

            jql_arq = f'project = {PROJECT_KEY} AND status = "In Progress C"'
            arq_tickets = jira_client.search_issues(jql_arq, maxResults=100)
            METRICS['pending_arq_overdue'].set(sum(1 for t in arq_tickets if count_business_days(t.fields.updated) > 1))
            
            METRICS['ready_for_prod'].set(jira_client.search_issues(f'project = {PROJECT_KEY} AND status = "IN PROGRESS D"', maxResults=0).total)
            METRICS['uat'].set(jira_client.search_issues(f'project = {PROJECT_KEY} AND status = "UAT"', maxResults=0).total)

            logging.info("Recolección de métricas completada.")

            jql_new_critical = f'project = {PROJECT_KEY} AND priority in (Highest, High) AND created >= "-5m"'
            new_critical_tickets = jira_client.search_issues(jql_new_critical)
            for ticket in new_critical_tickets:
                 component = ticket.fields.components[0].name if ticket.fields.components else "N/A"
                 alert_message = (f"🚨 *Nuevo Ticket Crítico*\n\n"
                                  f"<{JIRA_SERVER}/browse/{ticket.key}|{ticket.key}> - *{ticket.fields.summary}*\n"
                                  f"*Informador:* {ticket.fields.reporter.displayName}\n"
                                  f"*Componente:* {component}")
                 send_alert(alert_message)

            jql_critical_updated = f'project = {PROJECT_KEY} AND priority in (Highest, High) AND updated >= "-5m"'
            critical_updated_tickets = jira_client.search_issues(jql_critical_updated)
            for ticket in critical_updated_tickets:
                comments = jira_client.comments(ticket)
                if comments:
                    last_comment = comments[-1]
                    author_display_name = last_comment.author.displayName
                    if author_display_name not in INTERNAL_USERS and ALERTED_TICKETS["new_comment"].get(ticket.key) != last_comment.id:
                        alert_message = (f"⚠️ *Nuevo Comentario importante en Ticket Crítico*\n\n"
                                         f"<{JIRA_SERVER}/browse/{ticket.key}|{ticket.key}> - *{ticket.fields.summary}*\n"
                                         f"*Autor del Comentario:* {author_display_name}")
                        send_alert(alert_message)
                        ALERTED_TICKETS["new_comment"][ticket.key] = last_comment.id

            # --- CORREGIDO: Lógica de envío a Grafana ---
            logging.info("Enviando métricas a Grafana Cloud...")
            
            # Se crea un manejador de autenticación con las credenciales pre-cargadas.
            auth_handler = functools.partial(
                basic_auth_handler,
                username=GRAFANA_CLOUD_INSTANCE_ID,
                password=GRAFANA_CLOUD_API_KEY
            )

            push_to_gateway(
                gateway=f"{GRAFANA_CLOUD_URL}/push",
                job='jira_exporter_render',
                registry=registry,
                handler=auth_handler # Se usa el nuevo manejador
            )
            logging.info("Métricas enviadas con éxito.")

        except Exception as e:
            logging.error(f"Error durante el ciclo de recolección/envío: {e}", exc_info=True)

        logging.info("Ciclo completado. Durmiendo por 300 segundos...")
        time.sleep(300)

# --- Configuración del Servidor Web Flask ---
app = Flask(__name__)

@app.route('/')
def hello_world():
    """Esta es la ruta que Render usará para su health check."""
    return 'El worker de métricas de Jira está corriendo en segundo plano. ¡Todo OK!'

# --- Bucle Principal ---
if __name__ == '__main__':
    metrics_thread = threading.Thread(target=metrics_and_alerts_loop, daemon=True)
    metrics_thread.start()

    port = int(os.environ.get('PORT', 10000))
    logging.info(f"Iniciando servidor web en el puerto {port} para los health checks de Render...")
    app.run(host='0.0.0.0', port=port)

