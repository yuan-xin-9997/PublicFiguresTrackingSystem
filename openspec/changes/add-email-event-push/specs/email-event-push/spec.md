## ADDED Requirements

### Requirement: Configurable email delivery channel
The system SHALL provide an email delivery channel whose SMTP host, port, transport security, username, password source, sender identity, recipients, subject prefix, maximum events per message, timeout, worker interval, and retry policy are configurable without hard-coded environment values.

#### Scenario: Use file configuration
- **WHEN** no page override exists and valid email settings are present in `app.json` or referenced environment variables
- **THEN** the system uses those settings as the effective email channel configuration

#### Scenario: Prefer page configuration by field
- **WHEN** an administrator saves one or more non-empty email fields on the push management page
- **THEN** each saved field overrides the corresponding file or environment value while unsaved fields continue to inherit their lower-priority values

#### Scenario: Clear a page override
- **WHEN** an administrator explicitly clears a page override
- **THEN** the effective value falls back to the corresponding environment, `app.json`, or default value and the API reports the new source

#### Scenario: Reject invalid channel settings
- **WHEN** an administrator submits an invalid port, email address, transport security mode, timeout, or retry value
- **THEN** the system rejects the update with field-level validation errors and preserves the previous effective configuration

### Requirement: Secure credential handling
The system MUST protect SMTP credentials at rest, in transit, in APIs, logs, audit records, configuration views, and backups produced by the documented default workflow.

#### Scenario: Store a password from the page
- **WHEN** an administrator submits a new SMTP password and a valid external credential-encryption key is configured
- **THEN** the system encrypts the password before writing it to SQLite and never returns the plaintext value

#### Scenario: Missing credential-encryption key
- **WHEN** an administrator submits a page SMTP password without a valid external credential-encryption key
- **THEN** the system rejects the password update with a safe remediation message while allowing non-secret fields to remain unchanged

#### Scenario: Use an environment password
- **WHEN** no page password is stored and `password_env` names a configured environment variable
- **THEN** the system reads the SMTP password from that environment variable at send time without exposing its value

#### Scenario: Read masked configuration
- **WHEN** any authorized user reads email configuration or delivery errors
- **THEN** the response exposes only masked values, configured-state indicators, safe error summaries, and per-field configuration sources

### Requirement: Task and event type push rules
The system SHALL allow administrators to create, update, enable, disable, and delete push rules that select one or more existing collection tasks and one or more supported event types.

#### Scenario: Create a valid rule
- **WHEN** an administrator selects enabled or disabled collection tasks, selects at least one of `itinerary`, `statement`, or `other`, names the rule, and saves it
- **THEN** the system persists the rule and its exact task and event type selections

#### Scenario: Reject an empty selection
- **WHEN** a rule contains no task or no event type
- **THEN** the system rejects the rule with a validation error

#### Scenario: Disabled rule
- **WHEN** a matching collection task completes while its otherwise matching rule is disabled
- **THEN** that rule does not contribute any event to an email delivery batch

#### Scenario: Deleted collection task
- **WHEN** a selected collection task is deleted through an allowed cascade or becomes unavailable
- **THEN** the rule no longer matches that task and existing delivery history remains queryable

### Requirement: Incremental event selection
The system SHALL enqueue only events first created by a collection task run after an applicable rule is enabled and whose event type matches that rule.

#### Scenario: New matching event
- **WHEN** a selected task run creates a new timeline event whose type is selected by an enabled rule
- **THEN** the system records the task-run-to-event association and enqueues that event for every effective recipient

#### Scenario: Existing deduplicated event
- **WHEN** a selected task run adds evidence to an event that already existed before that run
- **THEN** the system does not enqueue that event as an incremental notification

#### Scenario: Non-matching event type
- **WHEN** a selected task creates an event whose type is not selected by any enabled rule for that task
- **THEN** the system does not enqueue that event

#### Scenario: Rule enabled after a run
- **WHEN** an administrator enables or creates a rule after a task run has completed
- **THEN** the system does not backfill events from that or any earlier run

#### Scenario: Manual or maintenance event processing
- **WHEN** an event is created or modified outside a selected collection task run
- **THEN** the system does not enqueue it under task-based incremental rules

### Requirement: Idempotent batched delivery
The system MUST persist delivery batches and items with database uniqueness constraints so overlapping rules, task retries, worker restarts, and repeated enqueue attempts do not create duplicate event entries for the same recipient and task run.

#### Scenario: Overlapping rules
- **WHEN** two enabled rules match the same task run, event, and recipient
- **THEN** the event appears once in that recipient's delivery batch

#### Scenario: Repeat enqueue
- **WHEN** the enqueue operation is executed more than once for the same completed task run
- **THEN** existing batch and item records are reused and no duplicate item is created

#### Scenario: Multiple recipients
- **WHEN** two distinct recipients are configured and an event matches
- **THEN** the system creates one independently tracked recipient batch for each recipient

#### Scenario: No matching events
- **WHEN** a task run completes without newly created matching events
- **THEN** the system sends no email and records a zero-enqueued task log entry

### Requirement: Informative email content
The system SHALL send UTF-8 multipart email grouped by task run and recipient, with deterministic ordering and sufficient event context for the recipient to understand and verify each item.

#### Scenario: Render a delivery email
- **WHEN** a pending batch contains deliverable events
- **THEN** the email subject identifies the configured prefix, task, and event count, and the plain-text and HTML bodies list each event's type, person, title, summary, Beijing time, location, statuses, and concrete source

#### Scenario: Include event links
- **WHEN** `server.base_url` is configured with a valid externally reachable base URL
- **THEN** each event entry includes a link to its system detail view built from that configured base URL

#### Scenario: No external base URL
- **WHEN** `server.base_url` is empty
- **THEN** the email remains complete without inventing or hard-coding an absolute event link

#### Scenario: Event no longer deliverable
- **WHEN** an enqueued event is deleted or rejected before its batch is sent
- **THEN** the worker skips that item with a recorded safe reason and marks an empty resulting batch as skipped instead of sending an empty email

### Requirement: Recoverable delivery and retry
The system SHALL deliver email outside collection database transactions, track lifecycle state and attempts, retry transient failures with bounded backoff, and allow an administrator to retry a terminal failed batch.

#### Scenario: Successful send
- **WHEN** the SMTP server accepts a pending batch
- **THEN** the system marks the batch and its items sent with a timestamp and preserves the stable message identifier

#### Scenario: Transient SMTP failure
- **WHEN** a connection, timeout, or temporary SMTP error occurs before the maximum attempt count
- **THEN** the system marks the batch retrying, increments the attempt count, stores a sanitized error summary, and schedules the next attempt using configured backoff

#### Scenario: Retry limit reached
- **WHEN** a batch fails at the configured maximum attempt count
- **THEN** the system marks it failed, stops automatic attempts, and exposes the failure in delivery history and task-center context

#### Scenario: Application restart
- **WHEN** the application restarts with pending or retrying batches whose next-attempt time is due
- **THEN** the notification worker resumes processing them without requiring the originating collection task to run again

#### Scenario: Collection success with email failure
- **WHEN** email enqueueing or delivery fails after event data has committed
- **THEN** the collection task's persisted event data and collection status remain intact and the notification failure is tracked separately

#### Scenario: Manual retry
- **WHEN** an administrator retries a failed batch
- **THEN** the system resets it to a due retry state without creating duplicate delivery items

### Requirement: Push management authorization and audit
The system MUST integrate push management with existing login, page permission, administrator authorization, task logs, and audit logs.

#### Scenario: Administrator manages push
- **WHEN** an authenticated administrator changes email configuration or rules, sends a test email, or retries a delivery
- **THEN** the system performs the action and writes an audit record containing the action, object, result, actor, time, and a non-secret summary

#### Scenario: Authorized ordinary user views status
- **WHEN** an ordinary user has the `notifications` page permission
- **THEN** the user can view masked channel health, rule summaries, and paginated delivery history

#### Scenario: Ordinary user attempts a write
- **WHEN** an ordinary user calls a configuration, rule mutation, test-send, or retry endpoint
- **THEN** the system returns forbidden and makes no state change

#### Scenario: User lacks page permission
- **WHEN** a non-admin user without `notifications` page permission requests notification data or navigates to push management
- **THEN** the API returns forbidden and the page is not shown in navigation

#### Scenario: Task run enqueues notifications
- **WHEN** a collection task run finishes and notification candidates are evaluated
- **THEN** its task logs record candidate, enqueued, skipped, and notification error counts without credentials or full recipient lists

### Requirement: Testable and operable delivery
The system SHALL provide a safe administrator test-send operation, health/status visibility, Beijing-time presentation, and automated coverage suitable for the existing Jenkins pipeline.

#### Scenario: Send a test email
- **WHEN** an administrator requests a test using valid effective configuration
- **THEN** the system sends a clearly labeled test message without creating an event delivery item and audits the result

#### Scenario: Test with incomplete configuration
- **WHEN** an administrator requests a test while required effective fields or credentials are missing
- **THEN** the system reports the specific missing fields without attempting SMTP authentication

#### Scenario: Display delivery times
- **WHEN** a stored UTC delivery timestamp is shown in the push management page or email
- **THEN** the system displays Beijing time and retains the original machine-readable UTC value in API data

#### Scenario: Continuous integration
- **WHEN** the Jenkins pipeline validates this change
- **THEN** it runs backend unit and API tests with a mocked SMTP service, frontend tests and production build, and the existing smoke checks without contacting a real mail provider
