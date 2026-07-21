#!/bin/zsh
set -e
export DB_DSN="${DB_DSN:-host=localhost port=5432 dbname=taskintel user=$(whoami)}"
cd ~/dev/task-ledger/reconciler
curl -s http://localhost:11434/api/embeddings -d '{"model":"nomic-embed-text","prompt":"warm"}' > /dev/null
python3 run_reconcile.py --user aad-dev --dsn "$DB_DSN" --embed-model nomic-embed-text --judge-model mistral-small3.1 --write
psql -d taskintel -c "INSERT INTO decisions (proposal_id, packet_id, decision, decided_by, decided_at) SELECT p.proposal_id, p.packet_id, 'accept', 'aad-dev', now() FROM proposals p JOIN review_packets rp USING (packet_id) WHERE p.op='complete_task' AND p.target_task_id='task-devocr' ORDER BY rp.generated_at DESC LIMIT 1 ON CONFLICT DO NOTHING;"
psql -d taskintel -c "SELECT apply_task_event('task-devocr','aad-dev','completed','user_decision',(SELECT d.decision_id FROM decisions d JOIN proposals p USING (proposal_id) WHERE p.op='complete_task' AND p.target_task_id='task-devocr' ORDER BY d.decided_at DESC LIMIT 1),'{}'::jsonb);"
echo "Done - refresh the dashboard as aad-dev and trace the completion to its source."
