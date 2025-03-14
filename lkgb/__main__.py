import logging
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from lkgb import config
from lkgb.backend import HuggingFaceBackend, OllamaBackend
from lkgb.ontology import SlogertOntology
from lkgb.parser import Parser
from lkgb.reports import RunSummary
from lkgb.store import Store

# Set up logging format
log_formatter = logging.Formatter("%(asctime)s [%(levelname)-4.4s] (%(module)s) %(message)s")
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logging.getLogger().addHandler(console_handler)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Set the backend
if config.USE_OLLAMA_BACKEND:
    logger.info("Using Ollama backend")
    backend = OllamaBackend()
else:
    logger.info("Using HuggingFace backend")
    backend = HuggingFaceBackend()

# Ontology for the logs
logs_ontology = SlogertOntology(config.ONTOLOGY_PATH)

# Load the embeddings model
local_embeddings = backend.get_embeddings(model=config.EMBEDDINGS_MODEL)

# Create the vector store
store = Store(
    url=config.NEO4J_URL,
    username=config.NEO4J_USERNAME,
    password=config.NEO4J_PASSWORD,
    embeddings_model=local_embeddings,
    ontology=logs_ontology,
)

if config.CLEAR_STORE:
    store.clear()

# Create the parser model
parser_model = backend.get_parser_model(
    model=config.PARSER_MODEL,
    temperature=config.PARSER_TEMPERATURE,
)

parser = Parser(
    parser_model,
    store,
    logs_ontology,
    config.SELF_REFLECTION_STEPS,
)


def main() -> None:
    logger.info("Reading logs from %s", config.TEST_LOG_PATH)

    logs_df = pd.read_csv(config.TEST_LOG_PATH)
    # To prevent weird stuff with NaNs
    logs_df = logs_df.fillna("")

    reports = []
    for log in tqdm(logs_df["text"], desc="Processing logs", colour="blue"):
        report = parser.parse(log)
        reports.append(report)

    logger.info("Log parsing done.")

    with Path.open(config.TEST_OUT_PATH, "w") as out_file:
        out_file.write("text,template\n")
        for log in tqdm(logs_df["text"], desc=f"Writing outputs to {config.TEST_OUT_PATH}", colour="blue"):
            template = store.get_template(log)
            out_file.write(f"{log},{template}\n")

    logger.info("Output written.")

    summary = RunSummary(reports)

    logger.info("Run summary:")
    logger.info("- Logs parsed: %s", len(logs_df))
    logger.info("- Average time taken to find very similar logs: %s", summary.avg_very_similar_logs_time_taken())
    logger.info("- Percentage of memory matches: %s", summary.percentage_memory_matches())
    logger.info("- Average time taken to find similar logs: %s", summary.avg_similar_logs_time_taken())
    logger.info("- Average time taken to generate templates: %s", summary.avg_template_generation_time_taken())
    logger.info("- Average total time taken to parse each log: %s", summary.avg_total_time_taken())


if __name__ == "__main__":
    main()
