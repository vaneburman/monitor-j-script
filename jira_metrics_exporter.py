# jira_metrics_exporter.py
#
# Versión Final y Completa:
# 1. Mide el desempeño de Desarrolladores y QA (tiempos de ciclo, retrabajo).
# 2. Envía alertas en tiempo real para tickets críticos y comentarios nuevos.
# 3. Envía todas las métricas a Grafana Cloud usando el formato Remote Write.
#
import os
import time
import logging
import threading
from datetime import datetime, date, timedelta
import requests
from jira import JIRA
# Se importan todos los tipos de métricas necesarios
from prometheus_client import CollectorRegistry, Gauge, Histogram, Counter, Summary
from dotenv import load_dotenv
from flask import Flask

# --- Librerías para el formato Remote Write ---
import snappy
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

# --- Listas de Equipo ---
DEVELOPERS = ["Rodrigo Perdomo", "Benjamin Gonzalez", "Francisco Ziegler", "Franco Murabito", "Vanesa Burman"]
QA_TEAM = ["Agustin Godoy"]
INTERNAL_USERS = DEVELOPERS + QA_TEAM + ["Marcelo Santini"] # Se combinan para la lógica de alertas
logging.info(f"Equipo de Desarrollo a medir: {DEVELOPERS}")
logging.info(f"Equipo de QA a medir: {QA_TEAM}")
logging.info(f"Usuarios internos (no generan alertas de comentario): {INTERNAL_USERS}")


ALERTED_TICKETS = {"new_comment": {}}

# --- Funciones Auxiliares ---
def business_hours_between(start_dt, end_dt):
    """Calcula las horas hábiles entre dos datetimes."""
    days = 0
    current_date = start_dt.date()
    while current_date <= end_dt.date():
        if current_date.weekday() < 5: # Lunes a Viernes
            days += 1
        current_date += timedelta(days=1)
    return days * 8

def parse_jira_date(date_str):
    """Parsea fechas de Jira que pueden tener diferentes formatos."""
    try:
        return datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S.%f%z')
    except ValueError:
        return datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S%z')

def send_alert(message):
    if not GMAIL_CHAT_WEBHOOK: return
    try:
        requests.post(GMAIL_CHAT_WEBHOOK, json={'text': message}, timeout=10).raise_for_status()
        logging.info("Alerta enviada correctamente.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al enviar la alerta: {e}")

# --- Función de Envío (sin cambios) ---
def send_to_grafana_remote_write(registry):
    metric_families = registry.collect()
    write_request = WriteRequest()
    for family in metric_families:
        for s in family.samples:
            ts = TimeSeries(labels=[Label(name="__name__", value=s.name)])
            for ln, lv in s.labels.items(): ts.labels.append(Label(name=ln, value=lv))
            ts.samples.append(Sample(value=s.value, timestamp=int(time.time() * 1000)))
            write_request.timeseries.append(ts)
    compressed_data = snappy.compress(write_request.SerializeToString())
    headers = {'Content-Type': 'application/x-protobuf', 'Content-Encoding': 'snappy', 'X-Prometheus-Remote-Write-Version': '0.1.0'}
    response = requests.post(url=GRAFANA_PUSH_URL, auth=(GRAFANA_INSTANCE_ID, GRAFANA_API_KEY), data=compressed_data, headers=headers)
    response.raise_for_status()

# --- Lógica Principal de Métricas y Alertas ---
def metrics_and_alerts_loop():
    logging.info("Iniciando hilo de recolección de métricas...")
    
    try:
        jira_client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USER, JIRA_API_TOKEN))
        logging.info("Conexión con Jira establecida.")
    except Exception as e:
        logging.critical(f"No se pudo conectar a Jira: {e}. El hilo se detendrá.")
        return

    while True:
        logging.info("Iniciando ciclo de recolección de desempeño...")
        registry = CollectorRegistry()
        
        # --- MÉTRICAS DE DESEMPEÑO DE DESARROLLADORES ---
        dev_tickets_in_progress = Gauge('dev_tickets_in_progress_count', 'Cantidad de tickets en curso por desarrollador', ['developer'], registry=registry)
        dev_cycle_time = Summary('dev_cycle_time_hours', 'Tiempo (en horas hábiles) desde que un ticket pasa a "En Curso" hasta "Listo para Prod"', ['developer'], registry=registry)
        dev_rework_count = Counter('dev_rework_total', 'Cantidad de veces que un ticket vuelve de Test/ARQ a En Curso', ['developer'], registry=registry)

        # --- MÉTRICAS DE DESEMPEÑO DE QA ---
        qa_cycle_time = Histogram('qa_testing_time_days', 'Tiempo (en días hábiles) que un ticket pasa en Test', buckets=[1, 3, float('inf')], registry=registry)

        try:
            # --- Lógica para Desarrolladores ---
            for dev_name in DEVELOPERS:
                jql_current = f'project = {PROJECT_KEY} AND status = "EN CURSO" AND assignee = "{dev_name}"'
                dev_tickets_in_progress.labels(developer=dev_name).set(jira_client.search_issues(jql_current, maxResults=0).total)

                jql_closed = f'project = {PROJECT_KEY} AND status changed to "Listo para Prod" by ("{dev_name}") after -7d'
                closed_issues = jira_client.search_issues(jql_closed, expand="changelog", maxResults=100)

                for issue in closed_issues:
                    in_progress_time, ready_for_prod_time, rework_events = None, None, 0
                    for history in issue.changelog.histories:
                        for item in history.items:
                            if item.field == 'status':
                                if item.toString == 'EN CURSO': in_progress_time = parse_jira_date(history.created)
                                if item.toString == 'Listo para Prod' and not ready_for_prod_time: ready_for_prod_time = parse_jira_date(history.created)
                                if item.fromString in ['Test', 'In Progress C'] and item.toString == 'EN CURSO': rework_events += 1
                    if in_progress_time and ready_for_prod_time:
                        dev_cycle_time.labels(developer=dev_name).observe(business_hours_between(in_progress_time, ready_for_prod_time))
                    if rework_events > 0: dev_rework_count.labels(developer=dev_name).inc(rework_events)

            # --- Lógica para QA (Agustín Godoy) ---
            jql_qa_done = f'project = {PROJECT_KEY} AND status changed from "Test" by ("Agustin Godoy") after -7d'
            qa_done_issues = jira_client.search_issues(jql_qa_done, expand="changelog", maxResults=100)
            for issue in qa_done_issues:
                test_start_time, test_end_time = None, None
                for history in reversed(issue.changelog.histories):
                    for item in history.items:
                        if item.field == 'status':
                            if item.toString == 'Test' and not test_start_time: test_start_time = parse_jira_date(history.created)
                            if item.fromString == 'Test' and test_start_time and not test_end_time: test_end_time = parse_jira_date(history.created)
                if test_start_time and test_end_time:
                    qa_cycle_time.observe(business_hours_between(test_start_time, test_end_time) / 8)

            logging.info("Recolección de métricas de desempeño completada.")
            
            # --- LÓGICA DE ALERTAS EN TIEMPO REAL (RESTAURADA) ---
            logging.info("Buscando alertas en tiempo real...")
            # Alerta de Nuevos Tickets Críticos
            jql_new_critical = f'project = {PROJECT_KEY} AND priority in (Highest, High) AND created >= "-5m"'
            new_critical_tickets = jira_client.search_issues(jql_new_critical)
            for ticket in new_critical_tickets:
                 component = ticket.fields.components[0].name if ticket.fields.components else "N/A"
                 alert_message = (f"🚨 *Nuevo Ticket Crítico*\n\n"
                                  f"<{JIRA_SERVER}/browse/{ticket.key}|{ticket.key}> - *{ticket.fields.summary}*\n"
                                  f"*Informador:* {ticket.fields.reporter.displayName}\n"
                                  f"*Componente:* {component}")
                 send_alert(alert_message)

            # Alerta de Nuevos Comentarios de Externos
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
            logging.info("Búsqueda de alertas finalizada.")

            # --- Lógica de envío ---
            send_to_grafana_remote_write(registry)
            logging.info("Métricas de desempeño enviadas con éxito.")

        except Exception as e:
            logging.error(f"Error durante el ciclo de recolección/envío: {e}", exc_info=True)

        logging.info("Ciclo de desempeño completado. Durmiendo por 300 segundos...")
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
    logging.info(f"Iniciando servidor web en el puerto {port}...")
    app.run(host='0.0.0.0', port=port)

