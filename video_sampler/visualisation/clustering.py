from collections import defaultdict
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from sklearn.cluster import KMeans
from sklearn.manifold import TSNE
from tqdm import tqdm
from transformers import AutoFeatureExtractor, ResNetModel

from ..utils import batched


def build_feature_model(model_str: str):
    """Build a feature extraction model
    :param model_str: model name
    :return: tuple of (model, extractor)
    """
    extractor = AutoFeatureExtractor.from_pretrained(model_str)
    model = ResNetModel.from_pretrained(model_str)
    return model, extractor


def extract_features(
    model_str: str, image_folder: Path, mkey="pixel_values", batch_size: int = 8
):
    """Extract features from a folder of images
    :param model_str: model name
    :param image_folder: folder with images
    :param mkey: key for the pixel values
    :param batch_size: batch size
    :return: dict with keys "embeds" and "paths"
    """

    out_features = defaultdict(list)
    model, extractor = build_feature_model(model_str)
    with torch.no_grad():
        all_files = list(image_folder.iterdir())
        for batch in tqdm(
            batched(all_files, batch_size), total=len(all_files) // batch_size
        ):
            # load images
            batch_imgs = [Image.open(img_path).convert("RGB") for img_path in batch]
            # extract features
            batch_imgs = extractor(batch_imgs, return_tensors="pt")[mkey]
            batch_features = model(batch_imgs).pooler_output.squeeze()
            if len(batch) == 1:
                batch_features = batch_features.expand(1, -1)
            batch_features = torch.functional.F.normalize(batch_features, p=2, dim=1)
            out_features["embeds"].extend(batch_features)
            out_features["paths"].extend([img_path.name for img_path in batch])
    return out_features


def cluster_features(
    features,
    max_clusters=50,
):
    """Cluster features using t-SNE and KMeans
    :param features: dict with keys "embeds" and "paths"
    :param max_clusters: maximum number of clusters
    :return: tuple of (X, cluster_labels)
    """
    proj = TSNE(n_components=2, perplexity=35, metric="cosine")
    Xorg = np.asarray(features["embeds"])
    X = proj.fit_transform(Xorg)

    # take about 10% of the frame as the number of clusters
    n_clusters = min(int(0.1 * len(features["embeds"])), max_clusters)
    cluster_model = KMeans(n_clusters=n_clusters, random_state=0).fit(Xorg)
    return X, cluster_model.labels_
