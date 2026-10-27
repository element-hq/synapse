#     http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.


from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine


def run_create(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
) -> None:
    """
    This delta used to repoint the `event_stream_ordering_fkey` foreign keys added by
    delta 74/03 at `events.stream_ordering2`, so that they would survive the column
    swap performed by the `replace_stream_ordering_column` background update. It was an
    attempt to mitigate a painful race between foreground and background updates
    touching the `stream_ordering` column of the events table; more info can be found
    at https://github.com/matrix-org/synapse/issues/15677.

    It could never do so successfully, because the unique index that a foreign key on
    `stream_ordering2` requires is itself built by a background update
    (`index_stream_ordering2`), and every delta runs before any background update does.
    Whenever this delta had work to do it therefore failed with

        psycopg2.errors.InvalidForeignKey: there is no unique constraint matching
        given keys for referenced table "events"

    which left any Postgres database older than schema version 60 unable to upgrade.

    `replace_stream_ordering_column` now drops and recreates these foreign keys itself,
    which works no matter which column they currently reference, so there is nothing
    left for this delta to do.
    """
