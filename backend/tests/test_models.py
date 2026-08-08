import unittest

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Experiment, ExperimentBlock, ExperimentVersion, User


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_experiment_versions_are_numbered_per_experiment(self):
        with Session(self.engine) as session:
            owner = User(
                username="researcher",
                password_hash="not-a-plaintext-password",
                role="researcher",
            )
            experiment = Experiment(name="Naming", owner=owner)
            session.add_all([owner, experiment])
            session.flush()

            session.add(
                ExperimentVersion(
                    experiment=experiment,
                    version_number=1,
                    storage_key=f"experiments/{experiment.id}/versions/1/package.zip",
                    original_filename="naming.zip",
                    sha256="a" * 64,
                    size_bytes=123,
                    created_by=owner.id,
                )
            )
            session.commit()

            self.assertEqual(len(experiment.versions), 1)
            self.assertEqual(experiment.versions[0].version_number, 1)

    def test_duplicate_experiment_version_number_is_rejected(self):
        with Session(self.engine) as session:
            owner = User(
                username="researcher",
                password_hash="not-a-plaintext-password",
                role="researcher",
            )
            experiment = Experiment(name="Naming", owner=owner)
            session.add_all([owner, experiment])
            session.flush()

            for suffix in ("a", "b"):
                session.add(
                    ExperimentVersion(
                        experiment=experiment,
                        version_number=1,
                        storage_key=f"experiments/{experiment.id}/{suffix}.zip",
                        original_filename=f"{suffix}.zip",
                        sha256=suffix * 64,
                        size_bytes=1,
                        created_by=owner.id,
                    )
                )

            with self.assertRaises(IntegrityError):
                session.commit()

    def test_block_positions_are_unique_within_an_experiment(self):
        with Session(self.engine) as session:
            owner = User(
                username="block-owner",
                password_hash="not-a-plaintext-password",
                role="researcher",
            )
            experiment = Experiment(name="Ordered Blocks", owner=owner)
            session.add_all([owner, experiment])
            session.flush()
            for suffix in ("a", "b"):
                session.add(
                    ExperimentBlock(
                        experiment=experiment,
                        position=0,
                        name=suffix,
                        storage_key=f"blocks/{suffix}.zip",
                        original_filename=f"{suffix}.zip",
                        sha256=suffix * 64,
                        size_bytes=1,
                        created_by=owner.id,
                    )
                )
            with self.assertRaises(IntegrityError):
                session.commit()


if __name__ == "__main__":
    unittest.main()
