use anyhow::{Context, Result, bail};
use serde::Deserialize;
use std::fs;

pub trait Classifier {
    /// Predict cluster index for a feature vector.
    fn predict(&self, features: &[f64]) -> usize;

    /// Number of clusters.
    fn n_clusters(&self) -> usize;
}

/// Load a classifier from a JSON model file.
/// Dispatches to the correct implementation based on the "algorithm" field.
pub fn load_model(path: &str) -> Result<Box<dyn Classifier>> {
    let content = fs::read_to_string(path)
        .with_context(|| format!("Failed to read model file: {}", path))?;
    let raw: serde_json::Value = serde_json::from_str(&content)
        .context("Failed to parse model JSON")?;

    let algorithm = raw["algorithm"]
        .as_str()
        .context("Missing 'algorithm' field in model JSON")?;

    match algorithm {
        "kmeans" => {
            let model: KMeansModel = serde_json::from_value(raw)
                .context("Failed to parse KMeans model")?;
            Ok(Box::new(KMeansClassifier::from_model(model)?))
        }
        _ => bail!("Unsupported algorithm: {}", algorithm),
    }
}

// --- KMeans ---

#[derive(Deserialize)]
struct KMeansScaler {
    mean: Vec<f64>,
    std: Vec<f64>,
}

#[derive(Deserialize)]
struct KMeansModel {
    n_clusters: usize,
    centroids: Vec<Vec<f64>>,
    scaler: KMeansScaler,
}

struct KMeansClassifier {
    n_clusters: usize,
    centroids: Vec<Vec<f64>>,
    mean: Vec<f64>,
    std: Vec<f64>,
}

impl KMeansClassifier {
    fn from_model(model: KMeansModel) -> Result<Self> {
        if model.centroids.len() != model.n_clusters {
            bail!(
                "Centroid count ({}) does not match n_clusters ({})",
                model.centroids.len(),
                model.n_clusters
            );
        }
        if model.scaler.mean.len() != model.scaler.std.len() {
            bail!("Scaler mean/std length mismatch");
        }
        Ok(Self {
            n_clusters: model.n_clusters,
            centroids: model.centroids,
            mean: model.scaler.mean,
            std: model.scaler.std,
        })
    }

    fn standardize(&self, features: &[f64]) -> Vec<f64> {
        features
            .iter()
            .zip(self.mean.iter().zip(self.std.iter()))
            .map(|(&x, (&m, &s))| if s != 0.0 { (x - m) / s } else { 0.0 })
            .collect()
    }
}

impl Classifier for KMeansClassifier {
    fn predict(&self, features: &[f64]) -> usize {
        let scaled = self.standardize(features);
        let mut best_cluster = 0;
        let mut best_dist = f64::MAX;

        for (i, centroid) in self.centroids.iter().enumerate() {
            let dist: f64 = scaled
                .iter()
                .zip(centroid.iter())
                .map(|(&a, &b)| (a - b) * (a - b))
                .sum();
            if dist < best_dist {
                best_dist = dist;
                best_cluster = i;
            }
        }
        best_cluster
    }

    fn n_clusters(&self) -> usize {
        self.n_clusters
    }
}
