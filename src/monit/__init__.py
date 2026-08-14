from monit.collector.collector import collect_metrics_json

def main() -> None:
    result = collect_metrics_json()
    print(result)