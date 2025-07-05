# jira_metrics_exporter.py
#
# Versión Final y Definitiva. El archivo prometheus_pb2.py es generado por build.sh
# para asegurar compatibilidad y seguir la especificación del Remote Write Endpoint.
#
import os
import time
import logging
import threading
from datetime import datetime, date, timedelta
import requests
from jira import JIRA
from prometheus_client import CollectorRegistry, Gauge
from dotenv import load_dotenv
from flask import Flask

# --- Librerías para el formato Remote Write ---
import snappy
# Este import ahora funcionará porque build.sh crea el archivo
from prometheus_pb2 import WriteRequest, TimeSeries, Label, Sample

# --- Configuración Inicial ---
load_dotenv() 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Variables de Configuración ---
JIRA_SERVER = os.getenv("JIRA_SERVER")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
PROJECT_KEY = "GRV"
GMAIL_CHAT_WEBHOOK = os.getenv("GMAIL_CHAT_WEBHOOK")
GRAFANA_PUSH_URL = os.getenv('GRAFANA_PUSH_URL') 
GRAFANA_INSTANCE_ID = os.getenv('GRAFANA_CLOUD_INSTANCE_ID')
GRAFANA_API_KEY = os.getenv('GRAFANA_CLOUD_API_KEY')

# --- Lista de Usuarios Internos ---
DEFAULT_INTERNAL_USERS = [
    "Marcelo Santini", "Vanesa Burman", "Francisco Ziegler",
    "Franco Murabito", "Rodrigo Perdomo", "Benjamin Gonzalez"
]
INTERNAL_USERS_STR = os.getenv("INTERNAL_USERS")
INTERNAL_USERS = [name.strip() for name in INTERNAL_USERS_STR.split(',')] if INTERNAL_USERS_STR else DEFAULT_INTERNAL_USERS
logging.info(f"Usuarios internos configurados: {INTERNAL_USERS}")

ALERTED_TICKETS = {"new_comment": {}}

# --- Funciones Auxiliares ---
def count_business_days(start_date_str):
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M:%S.%f%z').date()
    except ValueError:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M:%S%z').date()
    today = date.today()
    return len([d for d in (start_date + timedelta(days=i) for i in range((today - start_date).days)) if d.weekday() < 5])

def send_alert(message):
    if not GMAIL_CHAT_WEBHOOK: return
    try:
        requests.post(GMAIL_CHAT_WEBHOOK, json={'text': message}, timeout=10).raise_for_status()
        logging.info("Alerta enviada correctamente.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al enviar la alerta: {e}")
        
# --- Función de Envío Correcta ---
def send_to_grafana_remote_write(registry):
    """Convierte métricas al formato Protobuf, las comprime con Snappy y las envía."""
    metric_families = registry.collect()
    write_request = WriteRequest()
    
    for family in metric_families:
        for s in family.samples:
            time_series = TimeSeries()
            time_series.labels.append(Label(name="__name__", value=s.name))
            for label_name, label_value in s.labels.items():
                time_series.labels.append(Label(name=label_name, value=label_value))
            timestamp_ms = int(time.time() * 1000)
            time_series.samples.append(Sample(value=s.value, timestamp=timestamp_ms))
            write_request.timeseries.append(time_series)

    uncompressed_data = write_request.SerializeToString()
    compressed_data = snappy.compress(uncompressed_data)

    headers = {
        'Content-Type': 'application/x-protobuf',
        'Content-Encoding': 'snappy',
        'X-Prometheus-Remote-Write-Version': '0.1.0'
    }
    
    logging.info(f"Enviando {len(compressed_data)} bytes de datos comprimidos a: {GRAFANA_PUSH_URL}")
    response = requests.post(
        url=GRAFANA_PUSH_URL,
        auth=(GRAFANA_INSTANCE_ID, GRAFANA_API_KEY),
        data=compressed_data,
        headers=headers
    )
    response.raise_for_status()


# --- Lógica Principal de Métricas y Alertas ---
def metrics_and_alerts_loop():
    logging.info("Iniciando hilo de recolección de métricas...")
    
    if not all([JIRA_SERVER, JIRA_USER, JIRA_API_TOKEN, GRAFANA_PUSH_URL, GRAFANA_INSTANCE_ID, GRAFANA_API_KEY]):
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
            # Lógica de recolección de métricas
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
                    else: continue
                    break

            jql_arq = f'project = {PROJECT_KEY} AND status = "In Progress C"'
            arq_tickets = jira_client.search_issues(jql_arq, maxResults=100)
            METRICS['pending_arq_overdue'].set(sum(1 for t in arq_tickets if count_business_days(t.fields.updated) > 1))
            
            METRICS['ready_for_prod'].set(jira_client.search_issues(f'project = {PROJECT_KEY} AND status = "IN PROGRESS D"', maxResults=0).total)
            METRICS['uat'].set(jira_client.search_issues(f'project = {PROJECT_KEY} AND status = "UAT"', maxResults=0).total)

            logging.info("Recolección de métricas completada.")
            
            # --- LÓGICA DE ALERTAS COMPLETA ---
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

            # --- Lógica de envío final ---
            send_to_grafana_remote_write(registry)
            logging.info("Métricas enviadas con éxito.")

        except Exception as e:
            logging.error(f"Error durante el ciclo de recolección/envío: {e}", exc_info=True)

        logging.info("Ciclo completado. Durmiendo por 300 segundos...")
        time.sleep(300)

# --- Configuración del Servidor Web Flask ---
app = Flask(__name__)
@app.route('/')
def hello_world():
    return 'El worker de métricas de Jira está corriendo en segundo plano. ¡Todo OK!'

# --- Bucle Principal ---
if __name__ == '__main__':
    metrics_thread = threading.Thread(target=metrics_and_alerts_loop, daemon=True)
    metrics_thread.start()
    port = int(os.environ.get('PORT', 10000))
    logging.info(f"Iniciando servidor web en el puerto {port} para los health checks de Render...")
    app.run(host='0.0.0.0', port=port)

