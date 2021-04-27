from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional, Tuple
import logging
import pandas as pd

from .engineer import Engineer
from .label_downloader import RawLabels
from .processor import Processor
from .ee_exporter import LabelExporter, RegionExporter, Season
from .ee_boundingbox import BoundingBox
from src.constants import COUNTRY, CROP_PROB, LAT, LON, START, END, SOURCE, NUM_LABELERS, SUBSET

logger = logging.getLogger(__name__)

data_folder: Path = Path(__file__).parent.parent.parent / "data"


@dataclass
class Dataset:
    sentinel_dataset: str

    @staticmethod
    def is_output_folder_ready(output_folder: Path, check_if_folder_empty=False):
        if output_folder.exists():
            if check_if_folder_empty:
                is_empty = not any(output_folder.iterdir())
                return is_empty
            else:
                logger.warning(f"{output_folder} already exists skipping for {output_folder.stem}")
                return False
        elif output_folder.parent.exists():
            logger.info(f"Creating directory: {output_folder}")
            output_folder.mkdir()
            return True
        else:
            logger.warning(
                f"{output_folder.parent} does not exist locally, use `dvc pull data/{output_folder.parent.stem}` to download latest"
            )
            return False


@dataclass
class LabeledDataset(Dataset):
    dataset: str = ""
    country: str = ""

    # Raw label parameters
    raw_labels: Tuple[RawLabels, ...] = ()

    # Process parameters
    processors: Tuple[Processor, ...] = ()

    # Engineer parameters
    is_global: bool = False
    nan_fill: float = 0.0
    max_nan_ratio: float = 0.3
    checkpoint: bool = True
    add_ndvi: bool = True
    add_ndwi: bool = False
    include_extended_filenames: bool = True
    calculate_normalizing_dict: bool = True
    days_per_timestep: int = 30
    num_timesteps: int = 12

    def __post_init__(self):
        raw_dir = data_folder / "raw"
        self.raw_labels_dir = raw_dir / self.dataset
        self.raw_images_dir = raw_dir / self.sentinel_dataset
        self.labels_path = data_folder / "processed" / (self.dataset + ".csv")
        self.features_dir = data_folder / "features" / self.dataset

    def create_pickled_labeled_dataset(self):
        if self.is_output_folder_ready(self.features_dir):
            Engineer(
                sentinel_files_path=self.raw_images_dir,
                labels_path=self.labels_path,
                save_dir=self.features_dir,
                is_global=self.is_global,
                nan_fill=self.nan_fill,
                add_ndvi=self.add_ndvi,
                add_ndwi=self.add_ndwi,
                max_nan_ratio=self.max_nan_ratio,
            ).create_pickled_labeled_dataset(
                checkpoint=self.checkpoint,
                include_extended_filenames=self.include_extended_filenames,
                calculate_normalizing_dict=self.calculate_normalizing_dict,
                days_per_timestep=self.days_per_timestep,
            )

    def process_labels(self):
        total_days = timedelta(days=self.num_timesteps * self.days_per_timestep)

        # Combine all processed labels
        label_dfs = [p.process(self.raw_labels_dir, total_days) for p in self.processors]
        df = pd.concat(label_dfs)

        # Combine duplicate labels
        df[NUM_LABELERS] = 1
        df = df.groupby([LON, LAT, START, END], as_index=False).agg(
            {SOURCE: ",".join, CROP_PROB: "mean", NUM_LABELERS: "sum", SUBSET: "first"}
        )
        df[COUNTRY] = self.country
        df = df.reset_index(drop=True)
        df.to_csv(self.labels_path, index=False)

    def download_raw_labels(self):
        if len(self.raw_labels) == 0:
            logger.warning(f"No raw labels set for {self.dataset}")
        elif self.is_output_folder_ready(self.raw_labels_dir):
            for label in self.raw_labels:
                label.download_file(output_folder=self.raw_labels_dir)

    def export_earth_engine_data(self, start_from: Optional[int] = None):
        if self.is_output_folder_ready(self.raw_images_dir) or start_from is not None:
            LabelExporter(
                sentinel_dataset=self.sentinel_dataset,
                output_folder=self.raw_images_dir,
                monitor=False,
                fast=False,
            ).export(labels_path=self.labels_path, start_from=start_from)


@dataclass
class UnlabeledDataset(Dataset):
    region_bbox: BoundingBox
    season: Season

    def __post_init__(self):
        self.raw_images_dir = data_folder / "raw" / self.sentinel_dataset

    def export_earth_engine_data(self):
        if self.is_output_folder_ready(self.raw_images_dir):
            RegionExporter(
                sentinel_dataset=self.sentinel_dataset, output_folder=self.raw_images_dir
            ).export(region_bbox=self.region_bbox, season=self.season, metres_per_polygon=None)
