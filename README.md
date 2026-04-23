# Avito Data Pipeline

End-to-end data engineering project:
Scraping → Staging → Cleaning → Data Warehouse → BI + ML



# 🏠 Avito.ma — Data Pipeline

Pipeline de données complet pour les annonces immobilières Avito.ma.

```
Avito.ma → Scraping → Staging (PostgreSQL) → Clean → Data Warehouse (BI + ML)
```

---

## 🗂️ Structure du projet

```
data_pipeline/
├── data/
│   ├── bronze/        # JSON bruts issus du scraping
│   ├── silver/        # CSV nettoyés
│   └── gold/          # (réservé exports finaux)
├── logs/
│   └── pipeline.log
├── src/
│   ├── extract/
│   │   └── scraper.py         ← Selenium scraper (Avito.ma)
│   ├── staging/
│   │   └── load_staging.py    ← Chargement brut en base
│   ├── clean/
│   │   └── clean_data.py      ← Nettoyage + Feature Engineering
│   ├── warehouse/
│   │   ├── bi_schema.py       ← Star Schema (Power BI)
│   │   └── ml_schema.py       ← OBT Feature Store (ML)
│   ├── utils/
│   │   ├── db.py              ← Connexion PostgreSQL
│   │   └── logger.py          ← Logger centralisé
│   └── main.py                ← Orchestrateur du pipeline
├── .env                       ← Variables d'environnement
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## ⚙️ Installation

### 1. Cloner et préparer l'environnement

```bash
git clone <repo>
cd data_pipeline
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configurer les variables d'environnement

Copier `.env` et ajuster si nécessaire :
```
DB_HOST=
DB_PORT=
DB_NAME=
DB_USER=
DB_PASSWORD=
```

---

## 🐳 Lancement via Docker

### Démarrer uniquement PostgreSQL (recommandé pour le développement)

```bash
docker-compose up postgres -d
```

### Lancer le pipeline complet dans Docker

```bash
docker-compose up --build
```

---

## 🚀 Lancement local (avec Docker pour la DB)

```bash
# 1. Démarrer la base
docker-compose up postgres -d

# 2. Activer l'environnement
source venv/bin/activate

# 3. Lancer le pipeline
python src/main.py
```

---

## 🏗️ Architecture du Data Warehouse

### Schémas PostgreSQL

| Schéma | Rôle |
|---|---|
| `staging` | Données brutes temporaires |
| `clean` | Données nettoyées + features |
| `bi_schema` | Star Schema pour Power BI |
| `ml_schema` | OBT / Feature Store pour le ML |

### BI Schema (Star Schema)

```
fact_annonce
    ├── dim_localisation   (ville, quartier)
    ├── dim_caracteristiques (chambres, sdb, étage, année)
    └── dim_temps          (date, mois, trimestre, année)
```

### ML Schema (OBT)

```
feature_store
    → prix (target), surface_m2, nb_chambres, nb_salles_bain,
      ville, quartier, etage, annee_construction,
      prix_par_m2, age_bien, categorie_prix
```

> ⚠️ Les transformations ML (scaling, encoding, SMOTE…) se font **après** extraction depuis la base, dans le Brief ML.

---

## 🔄 Flux du pipeline

```
run_scraper()          → bronze/*.json
    ↓
run_staging()          → staging.raw_annonces
    ↓
run_clean()            → clean.annonces + silver/*.csv
    ↓
run_bi_schema()        → bi_schema.fact_annonce + dims
    ↓
run_ml_schema()        → ml_schema.feature_store
    ↓
_cleanup_staging()     → TRUNCATE staging.raw_annonces
```

Chaque étape dispose d'un **retry automatique** (3 tentatives, 10s d'intervalle).

---

## 🔌 Connexion Power BI

1. Ouvrir Power BI Desktop
2. **Obtenir des données** → PostgreSQL
3. Serveur : `localhost:5432`, Base : `avito_db`
4. Importer les tables du schéma `bi_schema`
5. Les relations sont déjà définies via les clés étrangères

---

## 🛡️ Conformité & RGPD

- ✅ Aucune donnée personnelle collectée (pas de nom, téléphone, email)
- ✅ Minimisation des données : uniquement les champs nécessaires à l'analyse
- ✅ Crawling poli : délai aléatoire 2–4s entre chaque requête
- ✅ Logs de traçabilité à chaque étape
- ✅ Données limitées aux annonces publiques immobilières
