#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


from sqlalchemy import Column
from sqlalchemy import Index
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table


def _has_uuid_index(table):
    for idx in table.indexes:
        if idx.columns.keys() == ['uuid']:
            return True
    return False


def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    for prefix in ('', 'shadow_'):
        table = Table(prefix + 'block_device_mapping', meta, autoload=True)
        new_column = Column('uuid', String(36), nullable=True)
        if not hasattr(table.c, 'uuid'):
            table.create_column(new_column)

        if prefix == '' and not _has_uuid_index(table):
            # Only add the index on the non-shadow table...
            index = Index('block_device_mapping_uuid_idx', table.c.uuid)

            index.create(migrate_engine)
