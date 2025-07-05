#
# Versión Optimizada para Grafana
# Mapeo de usuarios UNA SOLA VEZ al inicio
# Métricas actualizadas cada 5 minutos con datos consistentes
#
import os
import time
import logging
import threading
from datetime import datetime, date, timedelta
import requests
from jira import JIRA
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
DEVELOPER_NAMES_STR = os.getenv("DEVELOPER_LIST")
QA_NAMES_STR = os.getenv("QA_LIST")
PM_NAMES_STR = os.getenv("PM_LIST")

# Variables globales para mapas (se llenan UNA VEZ al inicio)
DEVELOPER_MAP = {}
QA_MAP = {}
PM_MAP = {}

ALERTED_TICKETS = {"new_comment": {}}

# --- Funciones Auxiliares ---
def business_hours_between(start_dt, end_dt):
    days = sum(1 for i in range((end_dt.date() - start_dt.date()).days + 1) if (start_dt.date() + timedelta(days=i)).weekday() < 5)
    return days * 8

def parse_jira_date(date_str):
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

def build_user_map_once(jira_client, names_str, map_name):
    """Construye un mapa de Account ID -> Display Name SOLO UNA VEZ al inicio."""
    user_map = {}
    if names_str:
        names = [name.strip() for name in names_str.split(',')]
        logging.info(f"🔍 Mapeando usuarios {map_name}: {names}")
        for name in names:
            try:
                users = jira_client.search_users(query=name, maxResults=1)
                if users:
                    user = users[0]
                    user_map[user.accountId] = user.displayName
                    logging.info(f"  ✅ {user.displayName} -> {user.accountId}")
                else:
                    logging.warning(f"  ❌ No encontrado: '{name}'")
            except Exception as e:
                logging.error(f"  ❌ Error buscando '{name}': {e}")
        logging.info(f"🎯 {map_name} final: {len(user_map)} usuarios mapeados")
    else:
        logging.warning(f"⚠️ Variable de entorno para {map_name} está vacía")
    return user_map

def send_to_grafana_remote_write(registry):
    """Envía métricas a Grafana usando Remote Write."""
    metric_families = registry.collect()
    write_request = WriteRequest()
    
    for family in metric_families:
        for s in family.samples:
            ts = TimeSeries(labels=[Label(name="__name__", value=s.name)])
            for ln, lv in s.labels.items(): 
                ts.labels.append(Label(name=ln, value=lv))
            ts.samples.append(Sample(value=s.value, timestamp=int(time.time() * 1000)))
            write_request.timeseries.append(ts)
    
    compressed_data = snappy.compress(write_request.SerializeToString())
    headers = {
        'Content-Type': 'application/x-protobuf', 
        'Content-Encoding': 'snappy', 
        'X-Prometheus-Remote-Write-Version': '0.1.0'
    }
    
    response = requests.post(
        url=GRAFANA_PUSH_URL, 
        auth=(GRAFANA_INSTANCE_ID, GRAFANA_API_KEY), 
        data=compressed_data, 
        headers=headers
    )
    response.raise_for_status()

# --- Lógica Principal de Métricas ---
def metrics_collection_loop(jira_client):
    """
    Bucle principal que SOLO recolecta métricas cada 5 minutos.
    Los mapas de usuarios ya están construidos y NO se modifican.
    """
    
    # Construir lista de usuarios internos UNA VEZ
    internal_user_ids = list(DEVELOPER_MAP.keys()) + list(QA_MAP.keys()) + list(PM_MAP.keys())
    internal_user_names = list(DEVELOPER_MAP.values()) + list(QA_MAP.values()) + list(PM_MAP.values())
    
    logging.info(f"👥 Usuarios internos: {internal_user_names}")
    
    # Información de debug UNA VEZ
    logging.info("🔍 MAPEO DE DESARROLLADORES:")
    for acc_id, dev_name in DEVELOPER_MAP.items():
        logging.info(f"  {dev_name} -> {acc_id}")

    cycle_count = 0
    
    while True:
        cycle_count += 1
        logging.info(f"📊 Ciclo #{cycle_count} - Recolectando métricas...")
        
        # Crear registro limpio para cada ciclo
        registry = CollectorRegistry()
        
        # Definir métricas
        dev_tickets_in_progress = Gauge(
            'dev_tickets_in_progress_count', 
            'Tickets en curso por desarrollador', 
            ['developer'], 
            registry=registry
        )
        
        dev_avg_time_in_progress = Gauge(
            'dev_avg_time_in_progress_hours',
            'Tiempo promedio en estado EN CURSO (horas)',
            ['developer'],
            registry=registry
        )
        
        dev_cycle_time = Summary(
            'dev_cycle_time_hours', 
            'Tiempo desde EN CURSO hasta Listo para Prod', 
            ['developer'], 
            registry=registry
        )
        
        dev_rework_count = Counter(
            'dev_rework_total', 
            'Tickets devueltos de Test/ARQ a EN CURSO', 
            ['developer'], 
            registry=registry
        )
        
        qa_cycle_time = Histogram(
            'qa_testing_time_days', 
            'Tiempo en estado Test (días)', 
            buckets=[1, 2, 3, 5, float('inf')], 
            registry=registry
        )

        try:
            # --- MÉTRICAS DE DESARROLLADORES ---
            logging.info("🔍 Recolectando métricas de desarrolladores...")
            
            for acc_id, dev_name in DEVELOPER_MAP.items():
                # 1. Tickets en curso
                jql_current = f'project = {PROJECT_KEY} AND status = "In Progress" AND assignee = "{acc_id}"'
                current_tickets = jira_client.search_issues(jql_current, maxResults=50)
                ticket_count = len(current_tickets)
                
                logging.info(f"  📈 {dev_name}: {ticket_count} tickets en curso")
                dev_tickets_in_progress.labels(developer=dev_name).set(ticket_count)
                
                # 2. Tiempo promedio en estado EN CURSO
                if current_tickets:
                    total_hours = 0
                    for ticket in current_tickets:
                        # Buscar cuándo entró en EN CURSO
                        changelog = jira_client.issue(ticket.key, expand='changelog').changelog
                        for history in reversed(changelog.histories):
                            for item in history.items:
                                if item.field == 'status' and item.toString == 'In Progress':
                                    start_time = parse_jira_date(history.created)
                                    hours_in_progress = (datetime.now(start_time.tzinfo) - start_time).total_seconds() / 3600
                                    total_hours += hours_in_progress
                                    break
                    
                    if total_hours > 0:
                        avg_hours = total_hours / len(current_tickets)
                        dev_avg_time_in_progress.labels(developer=dev_name).set(avg_hours)
                        logging.info(f"  ⏱️ {dev_name}: {avg_hours:.1f}h promedio en curso")
                
                # 3. Cycle time y rework (últimos 7 días)
                jql_recent = f'project = {PROJECT_KEY} AND assignee = "{acc_id}" AND updated >= -7d'
                recent_issues = jira_client.search_issues(jql_recent, expand="changelog", maxResults=100)
                
                for issue in recent_issues:
                    in_progress_time, ready_for_prod_time, rework_events = None, None, 0
                    
                    for history in issue.changelog.histories:
                        for item in history.items:
                            if item.field == 'status':
                                if item.toString == 'In Progress':
                                    in_progress_time = parse_jira_date(history.created)
                                if item.toString == 'IN PROGRESS D' and not ready_for_prod_time:
                                    ready_for_prod_time = parse_jira_date(history.created)
                                if item.fromString in ['TEST', 'In Progress C'] and item.toString == 'In Progress':
                                    rework_events += 1
                    
                    if in_progress_time and ready_for_prod_time:
                        cycle_hours = business_hours_between(in_progress_time, ready_for_prod_time)
                        dev_cycle_time.labels(developer=dev_name).observe(cycle_hours)
                    
                    if rework_events > 0:
                        dev_rework_count.labels(developer=dev_name).inc(rework_events)

            # --- MÉTRICAS DE QA ---
            logging.info("🔍 Recolectando métricas de QA...")
            
            if QA_MAP:
                qa_account_ids = ', '.join(f'"{acc_id}"' for acc_id in QA_MAP.keys())
                jql_qa_done = f'project = {PROJECT_KEY} AND status changed from "TEST" by ({qa_account_ids}) after -7d'
                qa_done_issues = jira_client.search_issues(jql_qa_done, expand="changelog", maxResults=100)
                
                logging.info(f"  📈 QA: {len(qa_done_issues)} tickets procesados en 7 días")
                
                for issue in qa_done_issues:
                    test_start_time, test_end_time = None, None
                    for history in reversed(issue.changelog.histories):
                        for item in history.items:
                            if item.field == 'status':
                                if item.toString == 'TEST' and not test_start_time:
                                    test_start_time = parse_jira_date(history.created)
                                if item.fromString == 'TEST' and test_start_time and not test_end_time:
                                    test_end_time = parse_jira_date(history.created)
                    
                    if test_start_time and test_end_time:
                        test_days = business_hours_between(test_start_time, test_end_time) / 8
                        qa_cycle_time.observe(test_days)

            # --- ALERTAS EN TIEMPO REAL ---
            logging.info("🚨 Verificando alertas...")
            
            # Tickets críticos nuevos
            jql_new_critical = f'project = {PROJECT_KEY} AND priority in (Highest, High) AND created >= "-5m"'
            new_critical_tickets = jira_client.search_issues(jql_new_critical)
            
            for ticket in new_critical_tickets:
                component = ticket.fields.components[0].name if ticket.fields.components else "N/A"
                alert_message = (
                    f"🚨 *Nuevo Ticket Crítico*\n\n"
                    f"<{JIRA_SERVER}/browse/{ticket.key}|{ticket.key}> - *{ticket.fields.summary}*\n"
                    f"*Informador:* {ticket.fields.reporter.displayName}\n"
                    f"*Componente:* {component}"
                )
                send_alert(alert_message)

            # Comentarios en tickets críticos
            jql_critical_updated = f'project = {PROJECT_KEY} AND priority in (Highest, High) AND updated >= "-5m"'
            critical_updated_tickets = jira_client.search_issues(jql_critical_updated)
            
            for ticket in critical_updated_tickets:
                comments = jira_client.comments(ticket)
                if comments:
                    last_comment = comments[-1]
                    if (last_comment.author.accountId not in internal_user_ids and 
                        ALERTED_TICKETS["new_comment"].get(ticket.key) != last_comment.id):
                        
                        alert_message = (
                            f"⚠️ *Nuevo Comentario en Ticket Crítico*\n\n"
                            f"<{JIRA_SERVER}/browse/{ticket.key}|{ticket.key}> - *{ticket.fields.summary}*\n"
                            f"*Autor:* {last_comment.author.displayName}"
                        )
                        send_alert(alert_message)
                        ALERTED_TICKETS["new_comment"][ticket.key] = last_comment.id

            # --- ENVÍO A GRAFANA ---
            send_to_grafana_remote_write(registry)
            logging.info("✅ Métricas enviadas a Grafana exitosamente")

        except Exception as e:
            logging.error(f"❌ Error en ciclo de métricas: {e}", exc_info=True)

        logging.info(f"😴 Ciclo #{cycle_count} completado. Durmiendo 5 minutos...")
        time.sleep(300)  # 5 minutos

# --- Configuración del Servidor Web Flask ---
app = Flask(__name__)

@app.route('/')
def hello_world():
    return 'Jira Metrics Worker está corriendo. ¡Todo OK!'

@app.route('/health')
def health_check():
    return {
        'status': 'OK',
        'developers': len(DEVELOPER_MAP),
        'qa_team': len(QA_MAP),
        'pm_team': len(PM_MAP)
    }

# --- INICIALIZACIÓN PRINCIPAL ---
if __name__ == '__main__':
    # 1. Conectar a Jira
    try:
        jira_client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USER, JIRA_API_TOKEN))
        logging.info("✅ Conexión con Jira establecida")
    except Exception as e:
        logging.critical(f"❌ Error conectando a Jira: {e}")
        exit(1)

    # 2. Mapear usuarios UNA SOLA VEZ
    logging.info("🔍 MAPEANDO USUARIOS (solo una vez al inicio)...")
    
    DEVELOPER_MAP = build_user_map_once(jira_client, DEVELOPER_NAMES_STR, "DEVELOPERS")
    QA_MAP = build_user_map_once(jira_client, QA_NAMES_STR, "QA")
    PM_MAP = build_user_map_once(jira_client, PM_NAMES_STR, "PM")

    # 3. Verificar que tenemos usuarios
    total_users = len(DEVELOPER_MAP) + len(QA_MAP) + len(PM_MAP)
    if total_users == 0:
        logging.critical("❌ No se encontraron usuarios. Verificar variables de entorno.")
        exit(1)
    
    logging.info(f"✅ Mapeo completado: {len(DEVELOPER_MAP)} devs, {len(QA_MAP)} QA, {len(PM_MAP)} PM")

    # 4. Iniciar hilo de métricas
    metrics_thread = threading.Thread(
        target=metrics_collection_loop, 
        args=(jira_client,), 
        daemon=True
    )
    metrics_thread.start()
    logging.info("🚀 Hilo de métricas iniciado")

    # 5. Iniciar servidor web
    port = int(os.environ.get('PORT', 10000))
    logging.info(f"🌐 Iniciando servidor web en puerto {port}")
    app.run(host='0.0.0.0', port=port)
