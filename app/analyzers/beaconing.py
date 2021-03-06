from configparser import NoOptionError

import numpy as np

from helpers.analyzer import Analyzer
from helpers.singletons import settings, es, logging
from collections import defaultdict
from collections import Counter
import helpers.utils

DEFAULT_MIN_TARGET_BUCKETS = 10

class BeaconingAnalyzer(Analyzer):

    def evaluate_model(self):
        self.extract_additional_model_settings()

        search_query = es.filter_by_query_string(self.model_settings["es_query_filter"])
        self.total_events = es.count_documents(search_query=search_query)

        logging.print_analysis_intro(event_type="evaluating " + self.model_name, total_events=self.total_events)
        logging.init_ticker(total_steps=self.total_events, desc=self.model_name + " - evaluating " + self.model_type + " model")

        eval_terms_array = defaultdict()
        total_terms_added = 0

        outlier_batches_trend = 0
        for doc in es.scan(search_query=search_query):
            logging.tick()
            fields = es.extract_fields_from_document(doc)

            try:
                target_sentences = helpers.utils.flatten_fields_into_sentences(fields=fields, sentence_format=self.model_settings["target"])
                aggregator_sentences = helpers.utils.flatten_fields_into_sentences(fields=fields, sentence_format=self.model_settings["aggregator"])
                will_process_doc = True
            except (KeyError, TypeError):
                logging.logger.debug("Skipping event which does not contain the target and aggregator fields we are processing. - [" + self.model_name + "]")
                will_process_doc = False

            if will_process_doc:
                observations = dict()

                for target_sentence in target_sentences:
                    flattened_target_sentence = helpers.utils.flatten_sentence(target_sentence)

                    for aggregator_sentence in aggregator_sentences:
                        flattened_aggregator_sentence = helpers.utils.flatten_sentence(aggregator_sentence)
                        eval_terms_array = self.add_term_to_batch(eval_terms_array, flattened_aggregator_sentence, flattened_target_sentence, observations, doc)

                total_terms_added += len(target_sentences)

            # Evaluate batch of events against the model
            last_batch = (logging.current_step == self.total_events)
            if last_batch or total_terms_added >= self.model_settings["batch_eval_size"]:
                logging.logger.info("evaluating batch of " + "{:,}".format(total_terms_added) + " terms")
                outliers = self.evaluate_batch_for_outliers(terms=eval_terms_array)

                if len(outliers) > 0:
                    unique_summaries = len(set(o.outlier_dict["summary"] for o in outliers))
                    logging.logger.info("total outliers in batch processed: " + str(len(outliers)) + " [" + str(unique_summaries) + " unique summaries]")
                    outlier_batches_trend += 1
                else:
                    logging.logger.info("no outliers detected in batch")
                    outlier_batches_trend -= 1

                # Reset data structures for next batch
                eval_terms_array = defaultdict()
                total_terms_added = 0

        self.print_analysis_summary()

    def extract_additional_model_settings(self):
        self.model_settings["target"] = settings.config.get(self.config_section_name, "target").replace(' ', '').split(",")  # remove unnecessary whitespace, split fields
        self.model_settings["aggregator"] = settings.config.get(self.config_section_name, "aggregator").replace(' ', '').split(",")  # remove unnecessary whitespace, split fields
        self.model_settings["trigger_sensitivity"] = settings.config.getint(self.config_section_name, "trigger_sensitivity")
        self.model_settings["batch_eval_size"] = settings.config.getint("beaconing", "beaconing_batch_eval_size")

        try:
            self.model_settings["min_target_buckets"] = settings.config.getint(self.config_section_name, "min_target_buckets")
        except NoOptionError:
            self.model_settings["min_target_buckets"] = DEFAULT_MIN_TARGET_BUCKETS

    @staticmethod
    def add_term_to_batch(eval_terms_array, aggregator_value, target_value, observations, doc):
        if aggregator_value not in eval_terms_array.keys():
            eval_terms_array[aggregator_value] = defaultdict(list)

        eval_terms_array[aggregator_value]["targets"].append(target_value)
        eval_terms_array[aggregator_value]["observations"].append(observations)
        eval_terms_array[aggregator_value]["raw_docs"].append(doc)

        return eval_terms_array

    def evaluate_batch_for_outliers(self, terms=None):
        # Initialize
        outliers = list()

        # In case we want to count terms within an aggregator, it's a bit easier.
        # For example:
        # terms["smsc.exe"][A, B, C, D, D, E]
        # terms["abc.exe"][A, A, B]
        # is converted into:
        # First iteration: "smsc.exe" -> counted_target_values: {A: 1, B: 1, C: 1, D: 2, E: 1}
        # For each aggregator, we iterate over all terms within it:
        # term_value_count for a document with term "A" then becomes "1" in the example above.
        # we then flag an outlier if that "1" is an outlier in the array ["1 1 1 2 1"]
        for _, aggregator_value in enumerate(terms):
            # Count percentage of each target value occuring
            counted_targets = Counter(terms[aggregator_value]["targets"])
            counted_target_values = list(counted_targets.values())

            logging.logger.debug("terms count for aggregator value " + aggregator_value + " -> " + str(counted_targets))

            if len(counted_targets) < self.model_settings["min_target_buckets"]:
                logging.logger.debug("less than " + str(self.model_settings["min_target_buckets"]) + " time buckets, skipping analysis")
                continue

            stdev = np.std(counted_target_values)
            logging.logger.debug("standard deviation: " + str(stdev))

            for term_counter, term_value in enumerate(terms[aggregator_value]["targets"]):
                term_value_count = counted_targets[term_value]

                if stdev < self.model_settings["trigger_sensitivity"]:
                    is_outlier = True
                else:
                    is_outlier = False

                if is_outlier:
                    outliers.append(self.prepare_and_process_outlier(stdev, term_value_count, terms, aggregator_value, term_counter))

        return outliers

    def prepare_and_process_outlier(self, decision_frontier, term_value_count, terms, aggregator_value, term_counter):
        # Extract fields from raw document
        fields = es.extract_fields_from_document(terms[aggregator_value]["raw_docs"][term_counter])

        observations = terms[aggregator_value]["observations"][term_counter]

        observations["aggregator"] = aggregator_value
        observations["term"] = terms[aggregator_value]["targets"][term_counter]
        observations["term_count"] = term_value_count
        observations["decision_frontier"] = decision_frontier
        observations["confidence"] = np.abs(decision_frontier - term_value_count)

        return self.process_outlier(fields, terms[aggregator_value]["raw_docs"][term_counter], extra_outlier_information=observations)
