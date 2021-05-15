from aggregators.FedMGDAPlus import FedMGDAPlusAggregator
from aggregators.AFA import AFAAggregator
from copy import deepcopy
from experiment.DefaultExperimentConfiguration import DefaultExperimentConfiguration
from torch import nn, device, Tensor
from client import Client
from logger import logPrint
from typing import Dict, List
import torch
from aggregators.Aggregator import Aggregator
from datasetLoaders.DatasetInterface import DatasetInterface
import matplotlib.pyplot as plt

# Group-Wise Aggregator based on clustering
# Even though it itself does not do aggregation, it makes programatic sense to inherit attributes and functions
class GroupWiseAggregation(Aggregator):
    def __init__(self, clients: List[Client], model: nn.Module, rounds: int, device: device, detectFreeRiders:bool, config: DefaultExperimentConfiguration, useAsyncClients:bool=False):
        super().__init__(clients, model, rounds, device, detectFreeRiders, useAsyncClients)

        self.config = config

        self.cluster_count = 3
        self.cluster_centres = None
        self.cluster_choices = [0]*len(self.clients)

        self.internalAggregator = self._init_aggregator(config.internalAggregator)
        self.externalAggregator = self._init_aggregator(config.externalAggregator)

    def trainAndTest(self, testDataset: DatasetInterface) -> Tensor:
        roundsError = torch.zeros(self.config.rounds)
        for r in range(self.config.rounds):
            logPrint("Round... ", r)
            self._shareModelAndTrainOnClients()
            models = self._retrieveClientModelsDict()

            # Perform Clustering
            self.clustering(models)
            print(self.cluster_choices)
            cluster_models = []
            cluster_model_count = []

            for choice in range(self.cluster_count):
                empty_model = deepcopy(self.model)
                indices = [val for val in self.cluster_choices if val == choice]
                cluster_model_count.append(len(indices))

                comb = 0.0
                for i in indices:
                    self._mergeModels(
                        models[i+1].to(self.config.device),
                        empty_model.to(self.config.device),
                        self.clients[i].p,
                        comb,
                    )
                    comb = 1.0

                cluster_models.append(empty_model)

            # Assume p value is based on size of cluster
            class FakeClient:
                def __init__(self, p:float, id:int):
                    self.p = p
                    self.id = id

            self.model = self.externalAggregator.aggregate([FakeClient(val/len(self.clients), i) for (i, val) in enumerate(cluster_model_count)], cluster_models)

            roundsError[r] = self.test(testDataset)

        return roundsError


    def _init_aggregator(self, aggregator: Aggregator) -> Aggregator:
        agg = aggregator(self.clients, self.model, self.rounds, self.device, self.detectFreeRiders)
        if isinstance(agg, AFAAggregator):
            agg.xi = self.config.xi
            agg.deltaXi = self.config.deltaXi
        elif isinstance(agg, FedMGDAPlusAggregator):
            agg.reinitialise(self.config.innerLR)

        return agg


    def _init_cluster_centres(self, models_weights: Tensor, models: Dict[int, nn.Module]) -> None:
        if self.cluster_centres == None:
            indices: Tensor = torch.randint(high=len(models_weights), size=(self.cluster_count,))
            self.cluster_centres = models_weights[indices]

        else:
            for choice in range(self.cluster_count):
                indices = [val for val in self.cluster_choices if val == choice]
                self.cluster_centres[choice] = self._gen_cluster_centre(indices, models)


    def _gen_cluster_centre(self, indices: Tensor, models: Dict[int, nn.Module]) -> Tensor:
        """ Takes the average of the clients assigned to each cluster to generate a new centre """
        # Here you should be using other robust aggregation algorithms to perform the centre calculation and blocking

        model = self.internalAggregator.aggregate([self.clients[i] for i in indices], [models[i+1] for i in indices])

        return self._generate_weights({0: model})[0]


    def _generate_weights(self, models: Dict[int, nn.Module]) -> Tensor:
        X = torch.tensor([]).to(self.device)
        for id, model in models.items():
            coords = torch.tensor([]).to(self.device)

            for name, param in model.named_parameters():
                coords = torch.cat((coords, param.data.view(-1)))

            X = torch.cat((X, coords))

        return X


    # I use Cosine Similarity to cluster as I believe it to be a better similarity
    # metric than just Euclidean distance
    def clustering(self, models: Dict[int, nn.Module]):
        models_weights = self._generate_weights(models)
        self._init_cluster_centres(models_weights, models)
        cos = nn.CosineSimilarity(0)

        old_centres = self.cluster_centres

        # While the clusters are still converging
        while cos(old_centres, self.cluster_centres) < 0.99:
            old_centres = self.cluster_centres

            for i, model in enumerate(models_weights):
                best_sim = 0
                choice = -1
                for j, cluster in enumerate(self.cluster_centres):
                    sim = cos(model, cluster)
                    if sim > best_sim:
                        best_sim = sim
                        choice = j

                self.cluster_choices[i] = choice

            self._init_cluster_centres(models_weights)

