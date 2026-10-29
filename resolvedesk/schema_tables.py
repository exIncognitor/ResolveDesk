"""Explicit application table ownership for migration and isolated sandbox cleanup."""
from resolvedesk.memory import MEMORY_TABLES
from resolvedesk.eval_data import EVAL_DATA_TABLES
from resolvedesk.eval_state import EVAL_STATE_TABLES
from resolvedesk.eval_retention import EVAL_RETENTION_TABLES
from resolvedesk.metrics import METRIC_TABLES
from resolvedesk.licenses import LICENSE_TABLES
from resolvedesk.knowledge_governance import GOVERNANCE_TABLES
from resolvedesk.identity import IDENTITY_TABLES
from resolvedesk.model_admin import MODEL_TABLES

APPLICATION_TABLES=IDENTITY_TABLES|MODEL_TABLES|MEMORY_TABLES|EVAL_DATA_TABLES|EVAL_STATE_TABLES|EVAL_RETENTION_TABLES|METRIC_TABLES|LICENSE_TABLES|GOVERNANCE_TABLES|{
    'runs','events','messages','tickets','feedback','checkpoints','turn_receipts','entity_state',
    'desk_assets','service_cases','service_audit','service_actions','desk_grants','grant_sources','grant_locks',
    'chunks','knowledge_documents','index_outbox','eval_jobs','regression_cases','quality_baselines','eval_queue_guard',
    'telemetry','desk_alerts','alert_outbox'}
