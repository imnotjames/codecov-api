from graphql_api.helpers.ariadne import ariadne_load_local_graphql

from .repository import repository_bindable

repository = ariadne_load_local_graphql(__file__, "repository.graphql")
