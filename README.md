# AWS + Databricks Samples

Sample solutions that combine **AWS** and **Databricks** to solve real customer problems.
This repository is the single, curated home for AWS + Databricks sample code: each subdirectory is a
self-contained sample with its own README and setup instructions. If a Databricks-related AWS sample
is not in this repository, it is not part of this collection.

## Samples

| Sample | What it shows |
|---|---|
| [`autonomous-retail-replenishment-genie-quick-mmf`](./autonomous-retail-replenishment-genie-quick-mmf) | A closed **detect → decide → act** replenishment loop: Databricks Many Model Forecasting (MMF) serving Amazon Chronos-2 for demand forecasts, a Databricks Genie Agent for natural-language surge detection, and Amazon Quick reconciling forecasts against live supplier availability in Amazon S3 Tables to place orders or raise exception tickets. Companion code for the AWS ML Blog and AWS Partner Network (APN) blog posts. |

## Repository layout

```
<sample-name>/          one self-contained sample per directory
  README.md             what it does, prerequisites, and step-by-step setup
  ...                    sample source, notebooks, infrastructure, and docs
```

Each sample directory is independent — start from that sample's own `README.md`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). New samples are added as a new top-level directory via pull
request and are reviewed by the maintainers listed in [CODEOWNERS](CODEOWNERS).

## Security

See [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) for how to report a security issue.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
