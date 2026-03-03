"""
Agent persona definitions for simulation.

Each persona has:
  name        — unique identifier
  persona     — description string passed to agent registration
  domain_tags — list of domain tags for the agent
  discoveries — list of discovery templates, each with:
      episodic    — past-tense first-person experience template
      semantic    — general fact template
      procedural  — action/rule template (always/never/should)
      params      — dict of placeholder → list[str] for random substitution

The MockAgent calls _generate_text(template, params) to fill in random values.
"""

PYTHON_DEV_PERSONA: dict = {
    "name": "alice-py-dev",
    "persona": "Senior Python developer focused on data engineering and API development",
    "domain_tags": ["python", "pandas", "api", "data"],
    "discoveries": [
        {
            "episodic": "I found that {library} {issue} while processing {dataset}",
            "semantic": "{library} {issue} in Python data pipelines",
            "procedural": "Always {fix} when working with {library} to avoid silent bugs",
            "params": {
                "library": ["pandas.read_csv", "pd.merge", "df.groupby", "numpy.reshape"],
                "issue": [
                    "silently coerces mixed-type columns to object dtype",
                    "drops NaN rows without a warning when merging on keys",
                    "returns incorrect results when the index is not reset",
                    "produces float64 columns even when input is integer",
                ],
                "dataset": ["client_data.csv", "transactions.csv", "user_export.csv", "logs.parquet"],
                "fix": [
                    "specify dtype explicitly to prevent silent coercion",
                    "reset the index after filtering or merging operations",
                    "validate column types immediately after loading data",
                    "set keep_default_na=False to control NaN handling",
                ],
            },
        },
        {
            "episodic": "I encountered a {error} error when {action} in the {module} module",
            "semantic": "{error} errors in Python occur when {cause}",
            "procedural": "To prevent {error} errors, {fix}",
            "params": {
                "error": ["KeyError", "ValueError", "AttributeError", "TypeError"],
                "action": [
                    "accessing dict keys without defaults",
                    "parsing JSON from an external API",
                    "calling methods on a None return value",
                    "joining dataframes with mismatched types",
                ],
                "module": ["data_loader", "api_client", "auth_handler", "transform"],
                "cause": [
                    "a key does not exist in the dictionary",
                    "the wrong data type is passed to a function",
                    "an object is None when a method is called",
                    "incompatible column types prevent the join",
                ],
                "fix": [
                    "use .get() with a default value instead of direct key access",
                    "add explicit type validation at function boundaries",
                    "check for None before calling methods on return values",
                    "cast columns to a common type before joining",
                ],
            },
        },
        {
            "episodic": "I noticed that {component} was {symptom} when running {task}",
            "semantic": "{component} performance degrades when {condition}",
            "procedural": "When {component} slows down, {optimization}",
            "params": {
                "component": [
                    "the database query", "the API endpoint",
                    "the batch processor", "the cache layer",
                ],
                "symptom": [
                    "taking over 10 seconds to respond",
                    "consuming excessive memory during processing",
                    "timing out intermittently under load",
                    "producing duplicate rows in the output",
                ],
                "task": [
                    "the nightly ETL job", "load testing",
                    "processing large CSV files", "running the full test suite",
                ],
                "condition": [
                    "the input size exceeds 100k rows",
                    "indexes are missing on the join columns",
                    "connections are not pooled and reused",
                    "all data is loaded into memory at once",
                ],
                "optimization": [
                    "add an index on the most frequently joined column",
                    "use connection pooling to reduce overhead",
                    "process data in chunks rather than loading everything at once",
                    "switch from iterrows to vectorised pandas operations",
                ],
            },
        },
        {
            "episodic": "I discovered that {tool} {behavior} when {context}",
            "semantic": "{tool} has a default that affects {impact}",
            "procedural": "When using {tool}, {best_practice}",
            "params": {
                "tool": ["pytest", "FastAPI", "asyncpg", "Pydantic", "httpx"],
                "behavior": [
                    "caches fixtures at module scope by default",
                    "validates request bodies before calling route handlers",
                    "requires the vector extension to be registered per connection",
                    "serialises datetimes as ISO strings without timezone info",
                    "follows redirects automatically up to 5 times",
                ],
                "context": [
                    "running tests in parallel across modules",
                    "handling large multipart uploads",
                    "querying pgvector columns without registration",
                    "returning timestamps from PostgreSQL",
                    "communicating with an API that redirects on auth",
                ],
                "impact": ["test isolation and repeatability", "API reliability", "query correctness"],
                "best_practice": [
                    "explicitly set the fixture scope to avoid cross-test contamination",
                    "set appropriate request and connection timeout values",
                    "always register the vector codec before running queries",
                    "use timezone-aware datetimes throughout the codebase",
                ],
            },
        },
        {
            "episodic": "I ran into a problem where {service} {problem} causing {effect}",
            "semantic": "{service} can {problem} when {condition}",
            "procedural": "To prevent {service} from {problem}, {prevention}",
            "params": {
                "service": [
                    "the Redis cache", "the Postgres connection pool",
                    "the background task runner", "the auth middleware",
                ],
                "problem": [
                    "ran out of connections under peak load",
                    "failed silently without raising an exception",
                    "blocked the event loop on a CPU-intensive task",
                    "returned stale data after a schema migration",
                ],
                "effect": [
                    "request timeouts during peak traffic",
                    "silent data corruption in the pipeline",
                    "degraded API response times for all users",
                    "incorrect responses for all users for 10 minutes",
                ],
                "condition": [
                    "connections are not properly closed after use",
                    "error handling is missing in async callbacks",
                    "blocking I/O is called from an async function",
                    "cache invalidation is not triggered after schema changes",
                ],
                "prevention": [
                    "always use context managers to ensure connections are closed",
                    "add explicit error handling and logging to async callbacks",
                    "use run_in_executor for CPU-intensive or blocking operations",
                    "implement cache versioning tied to schema version",
                ],
            },
        },
    ],
}

DEVOPS_PERSONA: dict = {
    "name": "bob-devops",
    "persona": "DevOps engineer managing Kubernetes clusters and CI/CD pipelines",
    "domain_tags": ["kubernetes", "docker", "ci-cd", "monitoring"],
    "discoveries": [
        {
            "episodic": "I found that {resource} {issue} when {event} in {environment}",
            "semantic": "{resource} {issue} in Kubernetes when {condition}",
            "procedural": "Always {fix} for {resource} to ensure reliability",
            "params": {
                "resource": ["pods", "deployments", "services", "config maps", "persistent volumes"],
                "issue": [
                    "failed to start due to OOMKilled errors",
                    "went into CrashLoopBackOff without clear logs",
                    "were evicted because the node ran out of disk space",
                    "lost connectivity after a node restart",
                ],
                "event": [
                    "scaling up under load", "deploying a new release",
                    "a node failure occurred", "the cluster autoscaler ran",
                ],
                "environment": ["production", "staging", "the dev cluster"],
                "condition": [
                    "memory limits are set too low",
                    "liveness probes are misconfigured",
                    "log rotation is not enabled on nodes",
                    "pod disruption budgets are not set",
                ],
                "fix": [
                    "set resource requests and limits on all containers",
                    "configure meaningful liveness and readiness probes",
                    "enable log rotation and set retention policies",
                    "define pod disruption budgets for critical services",
                ],
            },
        },
        {
            "episodic": "I discovered that the {pipeline} pipeline {failure} when {trigger}",
            "semantic": "CI/CD pipelines {failure} when {root_cause}",
            "procedural": "To avoid pipeline {failure}, {fix}",
            "params": {
                "pipeline": ["build", "test", "deploy", "security-scan", "integration-test"],
                "failure": [
                    "ran out of disk space mid-build",
                    "timed out waiting for the registry to respond",
                    "produced different results on re-runs",
                    "silently passed despite test failures",
                ],
                "trigger": [
                    "building large Docker images", "pulling images in parallel",
                    "using mutable image tags", "misconfiguring test exit codes",
                ],
                "root_cause": [
                    "build caches are not cleaned between runs",
                    "registry rate limits are hit under load",
                    "mutable tags like latest lead to non-determinism",
                    "test runners exit 0 even on failure",
                ],
                "fix": [
                    "add a cache cleanup step and set disk quotas on runners",
                    "use image pull secrets and implement retry logic",
                    "pin all image tags to specific SHA digests",
                    "always check the exit code of test commands explicitly",
                ],
            },
        },
        {
            "episodic": "I noticed that {service} had {symptom} in {environment}",
            "semantic": "{service} experiences {symptom} when {condition}",
            "procedural": "To detect {symptom} early, {monitoring_fix}",
            "params": {
                "service": ["the API gateway", "the database cluster", "the message queue", "the ingress controller"],
                "symptom": [
                    "elevated error rates over 1%",
                    "p99 latency above 2 seconds",
                    "memory usage growing without bound",
                    "connection refused errors from downstream services",
                ],
                "environment": ["production", "during the weekly batch job", "after the last deployment"],
                "condition": [
                    "upstream dependencies are slow or unavailable",
                    "connection pools are exhausted",
                    "memory leaks accumulate over days of uptime",
                    "misconfigured timeouts allow connections to hang",
                ],
                "monitoring_fix": [
                    "add SLO-based alerting with 5-minute burn rates",
                    "set up memory usage dashboards with trend alerts",
                    "monitor connection pool utilisation and alert at 80%",
                    "implement distributed tracing to identify slow spans",
                ],
            },
        },
        {
            "episodic": "I encountered a {problem} issue when {action} in {context}",
            "semantic": "{problem} occurs in container environments when {cause}",
            "procedural": "When dealing with {problem}, {solution}",
            "params": {
                "problem": [
                    "network policy", "secret rotation", "certificate expiry", "image pull",
                ],
                "action": [
                    "migrating services between namespaces",
                    "rotating database credentials",
                    "renewing TLS certificates for the ingress",
                    "deploying from a private registry in a new region",
                ],
                "context": [
                    "a production rollout", "a DR exercise",
                    "the quarterly security audit", "onboarding a new service",
                ],
                "cause": [
                    "network policies block cross-namespace traffic by default",
                    "applications cache credentials and do not reload them",
                    "certificate renewal is not automated",
                    "image pull secrets are not replicated across namespaces",
                ],
                "solution": [
                    "define explicit allow rules in network policies for cross-namespace services",
                    "implement graceful secret reloading without restarts",
                    "use cert-manager with automatic renewal well before expiry",
                    "use a Kubernetes operator to replicate secrets across namespaces",
                ],
            },
        },
        {
            "episodic": "I found that {component} {behavior} after {change}",
            "semantic": "{component} {behavior} when {condition} in production",
            "procedural": "Before making {change}, {precaution}",
            "params": {
                "component": [
                    "the service mesh", "the ingress controller",
                    "the horizontal pod autoscaler", "the cluster DNS",
                ],
                "behavior": [
                    "dropped connections for 30 seconds",
                    "stopped routing traffic to healthy pods",
                    "scaled down prematurely during high load",
                    "failed to resolve newly created services",
                ],
                "change": [
                    "upgrading the control plane",
                    "changing the load balancing algorithm",
                    "adjusting the HPA scale-down stabilisation window",
                    "restarting the CoreDNS pods",
                ],
                "condition": [
                    "the control plane is upgraded without draining nodes first",
                    "session affinity is required but not configured",
                    "the stabilisation window is too short for traffic patterns",
                    "DNS caches hold stale entries after a restart",
                ],
                "precaution": [
                    "drain and cordon nodes gradually to maintain availability",
                    "test the change in staging with production-like traffic",
                    "review the autoscaler history and adjust the stabilisation window",
                    "increase DNS TTLs before restarting CoreDNS",
                ],
            },
        },
    ],
}

DATA_SCIENTIST_PERSONA: dict = {
    "name": "carol-data-scientist",
    "persona": "Data scientist working on ML models for classification and forecasting",
    "domain_tags": ["ml", "sklearn", "training", "features", "evaluation"],
    "discoveries": [
        {
            "episodic": "I found that {model} {issue} when training on {dataset}",
            "semantic": "{model} {issue} in machine learning when {condition}",
            "procedural": "When using {model}, always {fix} to get reliable results",
            "params": {
                "model": ["RandomForest", "XGBoost", "LogisticRegression", "LSTM", "k-means"],
                "issue": [
                    "overfit to the training set with near-perfect accuracy",
                    "converged to a suboptimal local minimum",
                    "produced inconsistent results across runs",
                    "took 10x longer than expected to train",
                ],
                "dataset": [
                    "the churn prediction dataset", "the time series forecasting set",
                    "the imbalanced fraud detection data", "the high-dimensional feature set",
                ],
                "condition": [
                    "the model has too many parameters relative to training samples",
                    "the learning rate is too high for the loss landscape",
                    "random seeds are not fixed for reproducibility",
                    "the feature matrix is dense and not normalised",
                ],
                "fix": [
                    "use cross-validation and evaluate on a held-out test set",
                    "tune the learning rate with a scheduler",
                    "set a fixed random seed for reproducibility",
                    "normalise features before training distance-based models",
                ],
            },
        },
        {
            "episodic": "I noticed that {metric} was misleading when evaluating {model} on {dataset}",
            "semantic": "{metric} is misleading when {condition}",
            "procedural": "When the dataset is {condition}, use {better_metric} instead of {metric}",
            "params": {
                "metric": ["accuracy", "AUC-ROC", "MSE", "precision"],
                "model": ["the fraud detector", "the churn model", "the recommender", "the classifier"],
                "dataset": [
                    "heavily imbalanced classes", "a time series with trend",
                    "a sparse user interaction matrix", "multiclass data",
                ],
                "condition": [
                    "the classes are heavily imbalanced",
                    "the baseline is a trivial predictor",
                    "rare positive cases matter most",
                    "all classes have different business importance",
                ],
                "better_metric": [
                    "F1 score or precision-recall AUC",
                    "macro-averaged metrics across classes",
                    "weighted metrics with business-defined class weights",
                    "a calibration plot alongside discrimination metrics",
                ],
            },
        },
        {
            "episodic": "I discovered that {feature} caused {issue} in the {pipeline} pipeline",
            "semantic": "{feature} can cause {issue} in ML pipelines when {condition}",
            "procedural": "To avoid {issue} from {feature}, {fix}",
            "params": {
                "feature": [
                    "a datetime feature", "a high-cardinality categorical column",
                    "a feature with future leakage", "a raw text field",
                ],
                "issue": [
                    "target leakage that inflated validation scores",
                    "memory exhaustion during one-hot encoding",
                    "inconsistent encoding between training and inference",
                    "silent NaN propagation through the model",
                ],
                "pipeline": ["preprocessing", "training", "inference", "feature engineering"],
                "condition": [
                    "it was engineered using information not available at prediction time",
                    "it has thousands of unique values and is naively encoded",
                    "the encoding logic is duplicated in training and serving code",
                    "NaN handling is not explicit in the transformation step",
                ],
                "fix": [
                    "check feature timestamps against the prediction timestamp to detect leakage",
                    "use target encoding or embedding instead of one-hot for high cardinality",
                    "use sklearn Pipeline to ensure consistent transforms in training and serving",
                    "add explicit NaN imputation as the first step in the pipeline",
                ],
            },
        },
        {
            "episodic": "I ran into {problem} when deploying {model} to {environment}",
            "semantic": "{problem} is common when deploying ML models to {environment}",
            "procedural": "Before deploying {model}, {precaution}",
            "params": {
                "problem": [
                    "training-serving skew", "slow inference latency",
                    "model staleness after 2 weeks", "dependency version conflicts",
                ],
                "model": ["the XGBoost classifier", "the BERT embedding model", "the recommender system"],
                "environment": ["production", "the batch scoring job", "the real-time API"],
                "precaution": [
                    "compare training and serving feature distributions to detect skew",
                    "benchmark inference latency with production-size batches",
                    "set up automated retraining triggered by performance degradation",
                    "pin all dependency versions and test in a clean environment",
                ],
            },
        },
        {
            "episodic": "I found that {technique} {result} when applied to {task}",
            "semantic": "{technique} {result} for {task} when {condition}",
            "procedural": "When working on {task}, {recommendation}",
            "params": {
                "technique": [
                    "SMOTE oversampling", "feature selection via mutual information",
                    "learning rate warmup", "early stopping",
                    "ensemble averaging",
                ],
                "result": [
                    "significantly improved minority class recall",
                    "removed 40% of features without hurting performance",
                    "stabilised training for the first 10 epochs",
                    "prevented overfitting and reduced training time",
                    "reduced variance without noticeably increasing bias",
                ],
                "task": [
                    "imbalanced classification", "high-dimensional regression",
                    "fine-tuning a pretrained model", "deep learning on tabular data",
                    "combining multiple weak classifiers",
                ],
                "condition": [
                    "the minority class has fewer than 5% of samples",
                    "many features are redundant or correlated",
                    "the base learning rate is high relative to the model size",
                    "validation loss stops improving for 5+ epochs",
                    "each model has low correlation with the others",
                ],
                "recommendation": [
                    "try SMOTE before tuning the model to address class imbalance",
                    "run feature importance analysis before tuning hyperparameters",
                    "use a cosine learning rate schedule with linear warmup",
                    "always enable early stopping with a patience of 5-10 epochs",
                    "ensemble models that were trained with different seeds or architectures",
                ],
            },
        },
    ],
}

# Registry — easy to iterate over all personas
ALL_PERSONAS: list[dict] = [
    PYTHON_DEV_PERSONA,
    DEVOPS_PERSONA,
    DATA_SCIENTIST_PERSONA,
]
