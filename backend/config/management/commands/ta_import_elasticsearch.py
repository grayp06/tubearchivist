"""
Functionality:
- import the index of a running Elasticsearch, to migrate from
  an installation of the multi container version
- python manage.py ta_import_elasticsearch --url http://archivist-es:9200
"""

import os

import requests
from appsettings.src.es_import import ElasticImport
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """command framework"""

    # pylint: disable=no-member
    help = "Import all documents from a running Elasticsearch"

    def add_arguments(self, parser):
        """add arguments"""
        parser.add_argument(
            "--url",
            default=os.environ.get("ES_URL"),
            help="url of Elasticsearch, default from ES_URL",
        )
        parser.add_argument(
            "--username",
            default=os.environ.get("ELASTIC_USER", "elastic"),
            help="default from ELASTIC_USER or elastic",
        )
        parser.add_argument(
            "--password",
            default=os.environ.get("ELASTIC_PASSWORD"),
            help="default from ELASTIC_PASSWORD",
        )
        parser.add_argument(
            "--no-verify",
            action="store_true",
            help="do not verify the TLS certificate",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="overwrite documents in the local index",
        )

    def handle(self, *args, **options):
        """run import"""
        if not options["url"]:
            raise CommandError("--url or ES_URL is required")

        auth = None
        if options["password"]:
            auth = (options["username"], options["password"])

        handler = ElasticImport(
            options["url"],
            auth=auth,
            verify=not options["no_verify"],
            log=self.stdout.write,
        )
        try:
            version = handler.check_connection()
            self.stdout.write(f"connected to Elasticsearch {version}")
            result = handler.run(replace=options["replace"])
        except (ValueError, requests.RequestException) as err:
            raise CommandError(str(err)) from err

        total = sum(result.values())
        message = f"imported {total} documents, restart the application now"
        self.stdout.write(self.style.SUCCESS(message))
