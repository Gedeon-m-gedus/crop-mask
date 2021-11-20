from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple
from tqdm import tqdm
import logging
import pandas as pd
import numpy as np

from .engineer import Engineer
from .processor import Processor
from .ee_exporter import LabelExporter, RegionExporter, Season
from src.utils import data_dir, tifs_dir, features_dir, memoize
from src.ETL.ee_boundingbox import BoundingBox
from src.ETL.constants import (
    ALREADY_EXISTS,
    COUNTRY,
    CROP_PROB,
    FEATURE_FILENAME,
    FEATURE_PATH,
    LAT,
    LON,
    START,
    END,
    SOURCE,
    NUM_LABELERS,
    SUBSET,
    DATASET,
    TIF_PATHS,
)

logger = logging.getLogger(__name__)

unexported_file = data_dir / "unexported.txt"
unexported = pd.read_csv(unexported_file, sep="\n", header=None)[0].tolist()


@memoize
def generate_bbox_from_paths() -> Dict[Path, BoundingBox]:
    return {p: BoundingBox.from_path(p) for p in tqdm(tifs_dir.glob("**/*.tif"))}


@dataclass
class LabeledDataset:
    dataset: str = ""
    country: str = ""

    # Process parameters
    processors: Tuple[Processor, ...] = ()
    days_per_timestep: int = 30

    def __post_init__(self):
        self.raw_labels_dir = data_dir / "raw" / self.dataset
        self.labels_path = data_dir / "processed" / (self.dataset + ".csv")

    @staticmethod
    def merge_sources(sources):
        return ",".join(sources.unique())

    def process_labels(self):
        df = pd.DataFrame({})
        already_processed = []
        if self.labels_path.exists():
            df = pd.read_csv(self.labels_path)
            already_processed = df[SOURCE].unique()

        # Go through processors and create new labels if necessary
        new_labels = [
            p.process(self.raw_labels_dir, self.days_per_timestep)
            for p in self.processors
            if p.filename not in str(already_processed)
        ]

        if len(new_labels) == 0:
            return df

        df = pd.concat([df] + new_labels)

        # Combine duplicate labels
        df[NUM_LABELERS] = 1
        df = df.groupby([LON, LAT, START, END], as_index=False, sort=False).agg(
            {SOURCE: self.merge_sources, CROP_PROB: "mean", NUM_LABELERS: "sum", SUBSET: "first"}
        )
        df[COUNTRY] = self.country
        df[DATASET] = self.dataset

        df[FEATURE_FILENAME] = (
            "lat="
            + df[LAT].round(8).astype(str)
            + "_lon="
            + df[LON].round(8).astype(str)
            + "_date="
            + df[START].astype(str)
            + "_"
            + df[END].astype(str)
        )

        df = df.reset_index(drop=True)
        df.to_csv(self.labels_path, index=False)
        return df

    @staticmethod
    def get_tif_paths(path_to_bbox, lat, lon, start_date, end_date, pbar):
        candidate_paths = []
        for p, bbox in path_to_bbox.items():
            if bbox.contains(lat, lon) and p.stem.endswith(f"dates={start_date}_{end_date}"):
                candidate_paths.append(p)
        pbar.update(1)
        return candidate_paths

    def do_label_and_feature_amounts_match(self, labels: pd.DataFrame):
        all_subsets_correct_size = True
        if not labels[ALREADY_EXISTS].all():
            labels[ALREADY_EXISTS] = np.vectorize(lambda p: Path(p).exists())(labels[FEATURE_PATH])
        train_val_test_counts = labels[SUBSET].value_counts()
        for subset, labels_in_subset in train_val_test_counts.items():
            features_in_subset = labels[labels[SUBSET] == subset][ALREADY_EXISTS].sum()
            if labels_in_subset != features_in_subset:
                print(
                    f"\u2716 {subset}: {labels_in_subset} labels, but {features_in_subset} features"
                )
                all_subsets_correct_size = False
            else:
                print(f"\u2714 {subset} amount: {labels_in_subset}")
        return all_subsets_correct_size

    def generate_feature_paths(self, labels: pd.DataFrame) -> pd.Series:
        labels["feature_dir"] = str(features_dir)
        return labels["feature_dir"] + "/" + labels["filename"] + ".pkl"

    def match_labels_to_tifs(self, labels: pd.DataFrame) -> pd.Series:
        bbox_for_labels = BoundingBox(
            min_lon=labels[LON].min(),
            min_lat=labels[LAT].min(),
            max_lon=labels[LON].max(),
            max_lat=labels[LAT].max(),
        )
        # Get all tif paths and bboxes
        path_to_bbox = {
            p: bbox
            for p, bbox in generate_bbox_from_paths().items()
            if bbox_for_labels.overlaps(bbox)
        }

        # Match labels to tif files
        # Faster than going through bboxes
        with tqdm(total=len(labels), desc="Matching labels to tif paths") as pbar:
            tif_paths = np.vectorize(self.get_tif_paths, otypes=[np.ndarray])(
                path_to_bbox,
                labels[LAT],
                labels[LON],
                labels[START],
                labels[END],
                pbar,
            )
        return tif_paths

    def load_labels(
        self, allow_processing: bool = False, fail_if_missing_features: bool = False
    ) -> pd.DataFrame:
        if allow_processing:
            labels = self.process_labels()
        elif self.labels_path.exists():
            labels = pd.read_csv(self.labels_path)
        else:
            raise FileNotFoundError(f"{self.labels_path} does not exist")
        labels = labels[labels[CROP_PROB] != 0.5]
        labels = labels[~labels[FEATURE_FILENAME].isin(unexported)]
        labels["feature_dir"] = str(features_dir)
        labels[FEATURE_PATH] = labels["feature_dir"] + "/" + labels[FEATURE_FILENAME] + ".pkl"
        labels[ALREADY_EXISTS] = np.vectorize(lambda p: Path(p).exists())(labels[FEATURE_PATH])
        if fail_if_missing_features and not labels[ALREADY_EXISTS].all():
            raise FileNotFoundError(
                f"{self.dataset} has missing features: {labels[FEATURE_FILENAME].to_list()}"
            )
        return labels

    def create_features(self, disable_gee_export: bool = False):
        """
        Features are the (X, y) pairs that are used to train the model.
        In this case,
        - X is the satellite data for a lat lon coordinate over a 12 month time series
        - y is the crop/non-crop label for that coordinate

        To create the features:
        1. Obtain the labels
        2. Check if the features already exist
        3. Use the label coordinates to match to the associated satellite data (X)
        4. If the satellite data is missing, download it using Google Earth Engine
        5. Create the features (X, y)
        """
        print("------------------------------")
        print(self.dataset)

        # -------------------------------------------------
        # STEP 1: Obtain the labels
        # -------------------------------------------------
        labels = self.load_labels(allow_processing=True)

        # -------------------------------------------------
        # STEP 2: Check if features already exist
        # -------------------------------------------------
        labels_with_no_features = labels[~labels[ALREADY_EXISTS]].copy()
        if len(labels_with_no_features) == 0:
            self.do_label_and_feature_amounts_match(labels)
            return

        # -------------------------------------------------
        # STEP 3: Match labels to tif files (X)
        # -------------------------------------------------
        labels_with_no_features[TIF_PATHS] = self.match_labels_to_tifs(labels_with_no_features)
        tifs_found = labels_with_no_features[TIF_PATHS].str.len() > 0

        labels_with_no_tifs = labels_with_no_features.loc[~tifs_found]
        labels_with_tifs_but_no_features = labels_with_no_features.loc[tifs_found]

        # -------------------------------------------------
        # STEP 4: If no matching tif, download it
        # -------------------------------------------------
        if len(labels_with_no_tifs) > 0:
            print(f"{len(labels_with_no_tifs)} labels not matched")
            if not disable_gee_export:
                LabelExporter().export(labels=labels_with_no_tifs)

        # -------------------------------------------------
        # STEP 5: Create the features (X, y)
        # -------------------------------------------------
        if len(labels_with_tifs_but_no_features) > 0:
            Engineer().create_pickled_labeled_dataset(labels=labels_with_tifs_but_no_features)

        self.do_label_and_feature_amounts_match(labels)


@dataclass
class UnlabeledDataset:
    sentinel_dataset: str
    season: Season

    def export_earth_engine_data(self):
        RegionExporter(sentinel_dataset=self.sentinel_dataset).export(
            season=self.season, metres_per_polygon=None
        )
