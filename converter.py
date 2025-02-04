import json
import os
import time
import urllib
import openai
import requests
import re
from absl import app, flags, logging
from tqdm import tqdm


FLAGS = flags.FLAGS

flags.DEFINE_string(
    "prompt_file", default=None, help="Prompt file to use for the problem"
)

flags.DEFINE_string("input_file", default=None, help="Input file to read data")

flags.DEFINE_string("output_file", default=None, help="Output file to write to")

flags.DEFINE_integer("max_tokens", default=384, help="LM max generation length")

flags.DEFINE_integer("worker_id", default=0, help="Worker id for the job")

flags.DEFINE_integer("num_workers", default=1, help="number of workers")

flags.DEFINE_integer("batch_size", default=20, help="batch size for OpenAI queries")

flags.DEFINE_boolean(
    "geo_location", default=False, help="whether to add geo location to the output"
)

flags.DEFINE_string("info", default="address", help="address | intent")

flags.DEFINE_string("engine", "code-cushman-001", help="GPT engines")

GEO_BASE_URL = "https://maps.googleapis.com/maps/api/geocode/json?"

# TODO: add more keywords.
NON_ADDRESS_WORDS = [
    "arkadaş",
    "bebek",
    "enkaz",
    "deprem",
    "ekipman",
    "araç",
    "kayıp",
    "acil",
    "yardım",
    "kurtarma",
    "kayıp",
    "aile",
    "baba",
]


def postprocess_for_address(address):
    # a quick rule based filtering for badly parsed outputs.
    address = json.loads(address)
    if type(address) == dict:
        for key in (
            "mahallesi | bulvarı",
            "sokak | caddesi | yolu",
            "sitesi | apartmanı",
            "no | blok",
            "kat",
            "phone",
        ):
            if (
                key in address
                and len(address[key]) > 50
                or any(word in address[key] for word in NON_ADDRESS_WORDS)
            ):
                address[key] = ""

        for key in ("no | blok", "kat"):
            if key in address and len(address[key]) > 20:
                address[key] = ""

    return address


def postprocess_for_intent(intent):
    m = re.search(r"(?<=\[).+?(?=\])", intent)
    if m:
        return {"intent": m.group()}
    else:
        return {"intent": "unknown"}


def postprocess(info, line):
    if info == "address":
        return postprocess_for_address(line)
    elif info == "intent":
        return postprocess_for_intent(line)
    else:
        raise ValueError("Unknown info type")


def get_address_str(address):
    address_str = ""
    for key in (
        "mahallesi | bulvarı",
        "sokak | caddesi | yolu",
        "sitesi | apartmanı",
        "no | blok",
        "city",
        "province",
    ):
        address_str += address.get(key, "") + " "

    return address_str.strip()


def query_with_retry(inputs, max_retry=5):
    """Queries GPT API up to max_retry time to get the responses."""
    request_completed = False
    current_retry = 0
    outputs = [['{"status": "ERROR"}']] * len(inputs)
    while not request_completed and current_retry <= max_retry:
        try:
            response = openai.Completion.create(
                engine=FLAGS.engine,
                prompt=inputs,
                temperature=0.1,
                max_tokens=FLAGS.max_tokens,
                top_p=1,
                frequency_penalty=0.3,
                presence_penalty=0,
                stop="#END",
            )
            current_outputs = response["choices"]
            outputs = []
            for i in range(len(current_outputs)):
                outputs.append(
                    [
                        line
                        for line in current_outputs[i]["text"].split("\n")
                        if len(line) > 10
                    ]
                )
            request_completed = True
            logging.info("request completed")
        except openai.error.RateLimitError as error:
            logging.warning(f"Rate Limit Error: {error}")
            # wait for token limit in the API
            time.sleep(10 * current_retry)
            current_retry += 1
        except openai.error.InvalidRequestError as error:
            logging.warning(f"Invalid Request: {error}")
            # wait for token limit in the API
            time.sleep(10 * current_retry)
            current_retry += 1
    return outputs


def setup_openai():
    try:
        openai_keys = os.environ["OPENAI_API_KEY_POOL"].split(",")
    except KeyError:
        logging.error("OPENAI_API_KEY_POOL environment variable is not specified")

    assert len(openai_keys) > 0, "No keys specified in the environment variable"

    worker_openai_key = openai_keys[FLAGS.worker_id % len(openai_keys)].strip()
    openai.api_key = worker_openai_key


def setup_geocoding():
    try:
        geo_keys = os.environ["GEO_KEY_POOL"].split(",")
    except KeyError:
        logging.error("GEO_KEY_POOL environment variable is not specified")

    assert len(geo_keys) > 0, "No keys specified in the environment variable"

    worker_geo_key = geo_keys[FLAGS.worker_id % len(geo_keys)].strip()

    return worker_geo_key


def get_geo_result(key, address):
    address_str = get_address_str(address)
    parameters = {"address": address_str, "key": key}
    response = requests.get(f"{GEO_BASE_URL}{urllib.parse.urlencode(parameters)}")

    if response.status_code == 200:
        results = json.loads(response.content)["results"]
        if results:
            for result in results:
                if "geometry" in result and "location" in result["geometry"]:
                    loc = result["geometry"]["location"]
                    link = "https://maps.google.com/?q={lat},{lng}".format(
                        lat=loc["lat"], lng=loc["lng"]
                    )
                    result["gmaps_link"] = link
        return results
    else:
        logging.warning(response.content)


def main(_):
    setup_openai()
    if FLAGS.geo_location:
        geo_key = setup_geocoding()

    with open(FLAGS.prompt_file) as handle:
        template = handle.read()

    with open(FLAGS.input_file) as handle:
        raw_data = [json.loads(line.strip()) for line in handle]
        split_size = len(raw_data) // FLAGS.num_workers
        raw_data = raw_data[
            FLAGS.worker_id * split_size : (FLAGS.worker_id + 1) * split_size
        ]

    logging.info(f"Length of the data for this worker is {len(raw_data)}")
    text_inputs = []
    raw_inputs = []

    for index, row in tqdm(enumerate(raw_data)):
        text_inputs.append(template.format(ocr_input=row["Tweet"]))
        raw_inputs.append(row)

        if (index + 1) % FLAGS.batch_size == 0 or index == len(raw_data) - 1:
            outputs = query_with_retry(text_inputs)

            with open(FLAGS.output_file, "a+") as handle:
                for inp, output_lines in zip(raw_inputs, outputs):
                    for output_line in output_lines:
                        current_input = inp.copy()
                        try:
                            current_input[
                                FLAGS.info + "_json"
                            ] = postprocess(FLAGS.info, output_line)
                            current_input[FLAGS.info + "_str"] = ""
                        except Exception as e:
                            logging.warning(f"Parsing error in {output_line},\n {e}")
                            current_input[FLAGS.info + "_json"] = {}
                            current_input[FLAGS.info + "_str"] = output_line

                        if (
                            FLAGS.info == "address"
                            and FLAGS.geo_location
                            and type(current_input[FLAGS.info + "_json"]) == dict
                        ):
                            current_input["geo"] = get_geo_result(
                                geo_key, current_input[FLAGS.info + "_json"]
                            )

                        json_output = json.dumps(current_input)
                        handle.write(json_output + "\n")

            text_inputs = []
            raw_inputs = []


if __name__ == "__main__":
    app.run(main)
