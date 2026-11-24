/** @typedef {{id:number, title:string, payload:object, author:string}} BusinessCase */
/** @typedef {{id:number, case_id:number, status:'queued'|'running'|'complete'|'failed'}} Run */
/** @typedef {{id:number, run_id:number, digest:string, report:{summary:string, metrics:object, sections:Array}}} Evidence */
/** @typedef {'restored'|'already_complete'|'conflict'|'recoverable'|'not_found'} RecoveryOutcome */
/** @typedef {{outcome:RecoveryOutcome, run_id:number, run?:object, evidence?:object, detail?:string|object}} RecoveryResult */
/** @typedef {{id:number, run_id:number, case_id:number, created_at:string}} BackupRecord */
