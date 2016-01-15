__author__ = "Rafael Ferreira da Silva"

import logging
import datetime
import glob
import shutil
import time
import sys
import warnings

from Pegasus.db import connection
from Pegasus.db.schema import *
from sqlalchemy.orm.exc import *

log = logging.getLogger(__name__)

#-------------------------------------------------------------------
# DB Admin configuration
#-------------------------------------------------------------------
CURRENT_DB_VERSION = 6
DB_MIN_VERSION = 4

COMPATIBILITY = {
    '4.3.0': 1, '4.3.1': 1, '4.3.2': 1,
    '4.4.0': 2, '4.4.1': 2, '4.4.2': 2,
    '4.5.0': 4, '4.5.1': 4, '4.5.2': 4, '4.5.3': 4, '4.5.4': 5,
    '4.6.0': 6
}
#-------------------------------------------------------------------

class DBAdminError(Exception):
    pass


def get_compatible_version(version):
    print_version = None
    previous_version = None

    if version > CURRENT_DB_VERSION:
        pv = -1
        for ver in COMPATIBILITY:
            if COMPATIBILITY[ver] > pv and ver > previous_version:
                pv = COMPATIBILITY[ver]
                print_version = ver
                previous_version = ver

    for ver in COMPATIBILITY:
        if COMPATIBILITY[ver] == version and ver > previous_version:
            print_version = ver
            previous_version = ver
    return print_version


def get_class(version, db):
    module = "Pegasus.db.admin.versions.v%s" % version
    mod = __import__(module, fromlist=["Version"])
    klass = getattr(mod, "Version")
    return klass(db)


#-------------------------------------------------------------------
def db_create(dburi, engine, db, pegasus_version=None, force=False, verbose=True):
    """ Create/Update the Pegasus database from the schema """
    table_names = engine.table_names(connection=db)
    db_version.create(engine, checkfirst=True)

    v = -1
    if len(table_names) == 0:
        engine.execute(db_version.insert(), version=CURRENT_DB_VERSION,
                version_timestamp=datetime.datetime.now().strftime("%s"))
        if verbose:
            print "Created Pegasus database in: %s" % dburi
    else:
        v = _discover_version(db, pegasus_version=pegasus_version, force=force, verbose=False)
    
    try:
        metadata.create_all(engine)
    except OperationalError, e:
        raise DBAdminError(e)
    if verbose and v > 0:
        print "Your database has been updated."
            

def db_current_version(db, parse=False, force=False):
    """ Get the current version of the database."""
    current_version = None

    try:
        current_version = _get_version(db)
    except NoResultFound:
        current_version = _discover_version(db, force=force)

    if parse:
        if current_version > CURRENT_DB_VERSION:
            log.warn("You database was created with a newer Pegasus version, and may not be compatible with the current version.")
            return None

        current_version = get_compatible_version(current_version)
        if not current_version:
            raise DBAdminError("Your database is not compatible with any Pegasus version.\nRun 'pegasus-db-admin update %s' to update it to the latest version." % db.get_bind().url)

    return current_version


def db_verify(db, pegasus_version=None, force=False):
    """ Verify whether the database is compatible to the specified 
        Pegasus version."""
    _verify_tables(db)
    version = parse_pegasus_version(pegasus_version)

    try:
        compatible = _check_version(db, version)

    except NoResultFound:
        _discover_version(db, pegasus_version=pegasus_version, force=force)
        compatible = _check_version(db, version)
    
    if not compatible:
        raise DBAdminError("Your database is NOT compatible with version %s" % get_compatible_version(version))


def db_downgrade(db, pegasus_version=None, force=False, verbose=True):
    """ Downgrade the database. """
    if not check_table_exists(db, db_version):
        raise DBAdminError("Unable to determine database version.")

    try:
        current_version = _get_version(db)
    except NoResultFound:
        raise DBAdminError("Unable to determine database version.")

    if pegasus_version:
        version = parse_pegasus_version(pegasus_version)
    else:
        previous_version = ''
        for ver in COMPATIBILITY:
            if COMPATIBILITY[ver] < current_version and ver > previous_version:
                version = COMPATIBILITY[ver]
                previous_version = ver

    if current_version == version:
        log.info("Your database is already downgraded.")
        return
    elif current_version < version:
        raise DBAdminError("Cannot downgrade to a higher version.")
        return

    if version < DB_MIN_VERSION:
        raise DBAdminError("Your database is already downgraded to the minimum version.")

    _backup_db(db)
    for i in range(int(current_version), int(version) - 1, -1):

        if i == int(current_version):
            max_range = _get_minor_version(current_version)
        else:
            max_range = _get_max_minor_version(i)

        for j in range(max_range, 0, -1):
            k = get_class("%s-%s" % (i, j), db)
            k.downgrade(force)
            actual_version = float("%s.%s" % (i, j - 1))
            _update_version(db, actual_version)

        if (i > version):
            k = get_class(i, db)
            k.downgrade(force)
            actual_version = float("%s.%s" % (i - 1, _get_max_minor_version(i - 1)))
            _update_version(db, actual_version)

        if actual_version == version:
            break

    if verbose:
        print "Your database was successfully downgraded."


def parse_pegasus_version(pegasus_version=None):
    """

    :param pegasus_version:
    :return:
    """
    version = None
    if pegasus_version == 0 or pegasus_version:
        for key in COMPATIBILITY:
            if key == pegasus_version:
                version = COMPATIBILITY[key]
                break
        if not version:
            raise DBAdminError("Version does not exist: %s." % pegasus_version)

    if not version:
        version = CURRENT_DB_VERSION
    return version


def all_workflows_db(db, update=True, pegasus_version=None, schema_check=True, force=False):
    """
    Update/Downgrade all completed workflow databases listed in master_workflow table.
    :param db:
    :param pegasus_version:
    :param schema_check:
    :param force:
    :return:
    """
    db_urls = db.query(DashboardWorkflow.db_url).join(DashboardWorkflowstate).filter(DashboardWorkflowstate.state=="WORKFLOW_TERMINATED").all()

    # files
    file_prefix = "%s-dbadmin" % time.strftime("%Y%m%dT%H%M%S")
    f_out = open("%s.out" % file_prefix, 'w')
    f_err = open("%s.err" % file_prefix, 'w')

    counts = {
        'total': len(db_urls),
        'success': 0,
        'failed': 0,
        'unable_to_connect': 0,
    }
    if update:
        msg = ['updating', 'Updated']
    else:
        msg = ['downgrading', 'Downgraded']

    print ""
    print "Verifying and %s workflow databases:" % msg[0]
    i = 0
    for db_url in db_urls:
        dburi = db_url[0]
        log.debug("%s '%s'..." % (msg[0], dburi))
        i += 1
        sys.stdout.write("\r%d/%d" % (i, counts['total']))
        sys.stdout.flush()
        try:
            if update:
                con = connection.connect(dburi, pegasus_version=pegasus_version, schema_check=schema_check, create=True, force=force, verbose=False)
            else:
                con = connection.connect(dburi, schema_check=schema_check, create=False, verbose=False)
                metadata.clear()
                warnings.simplefilter("ignore")
                metadata.reflect(bind=con.get_bind())
                db_downgrade(con, pegasus_version=pegasus_version, force=force, verbose=False)
            con.close()
            f_out.write("[SUCCESS] %s\n" % dburi)
            counts['success'] += 1
        except connection.ConnectionError, e:
            if "unable to open database file" in str(e):
                f_err.write("[UNABLE TO CONNECT] %s\n" % dburi)
                counts['unable_to_connect'] += 1
                log.debug(e)
            else:
                f_err.write("[ERROR] %s\n" % dburi)
                counts['failed'] += 1
                log.debug(e)
        except Exception, e:
            f_err.write("[ERROR] %s\n" % dburi)
            counts['failed'] += 1
            log.debug(e)

    f_out.close()
    f_err.close()

    print "\n\nSummary:"
    print "  Verified/%s: %s/%s" % (msg[1], counts['success'], counts['total'])
    print "  Failed: %s/%s" % (counts['failed'], counts['total'])
    print "  Unable to connect: %s/%s" % (counts['unable_to_connect'], counts['total'])
    print "\nLog files:"
    print "  %s.out (Succeeded operations)" % file_prefix
    print "  %s.err (Failed operations)" % file_prefix


################################################################################
def _get_version(db):
    try:
        current_version = db.query(DBVersion.version).order_by(DBVersion.id.desc()).first()

    except OperationalError, e:
        # update dbversion table
        # Temporary migration. Should be removed in future releases
        try:
            log.info("Updating dbversion...")
            if db.get_bind().driver == "mysqldb":
                db.execute("RENAME TABLE dbversion TO dbversion_v4")
            else:
                db.execute("ALTER TABLE dbversion RENAME TO dbversion_v4")
            db_version.create(db.get_bind(), checkfirst=True)
            db.execute("INSERT INTO dbversion(version_number, version, version_timestamp) SELECT version_number, version_number, version_timestamp FROM dbversion_v4 ORDER BY id")
            db.execute("DROP TABLE dbversion_v4")
            db.commit()
            current_version = db.query(DBVersion.version).order_by(DBVersion.id.desc()).first()

        except (OperationalError, ProgrammingError), e:
            pass
        except Exception, e:
            db.rollback()
            raise DBAdminError(e)

    if not current_version:
        log.debug("No version record found on dbversion table.")
        raise NoResultFound()
    return float(current_version[0])


def _discover_version(db, pegasus_version=None, force=False, verbose=True):
    version = parse_pegasus_version(pegasus_version)

    current_version = 0
    if not force:
        try:
            current_version = _get_version(db)
        except NoResultFound:
            pass

    if current_version == version:
        try:
            _verify_tables(db)
            log.debug("Your database is already updated.")
            return None
        except DBAdminError:
            current_version = 0
    elif current_version > CURRENT_DB_VERSION:
        return 0

    if current_version > version:
        raise DBAdminError("Unable to run update. Current database version is newer than specified version '%s'." % (pegasus_version))
    
    _backup_db(db)
    v = 0.0
    for i in range(int(current_version), int(version) + 1):
        if not i == int(current_version):
            k = get_class(i, db)
            k.update(force=force)
        v = float(i)

        # verify minor versions
        max_range = 999
        if i == int(version):
            max_range = _get_minor_version(version)

        for j in range(1, max_range + 1):
            try:
                k = get_class("%s-%s" % (i, j), db)
                k.update(force=force)
                v = float("%s.%s" % (i, j))
            except ImportError:
                break

    if v > current_version:
        _update_version(db, v)
        if verbose:
            print "Your database has been updated."
    else:
        v = 0
    return v


def _check_version(db, version):
    db_version = _get_version(db)
    if db_version and db_version <= CURRENT_DB_VERSION and not version == db_version:
        return False
    return True


def _update_version(db, version):
    v = DBVersion()
    v.version = version
    v.version_timestamp = datetime.datetime.now().strftime("%s")
    if db:
        db.add(v)
        db.commit()


def _backup_db(db):
    url = db.get_bind().url
    if url.drivername == "sqlite":
        db_list = glob.glob(url.database + ".[0-9][0-9][0-9]")
        max_index = -1
        for file in db_list:
            index = int(file[-3:])
            if index > max_index:
                max_index = index
        dest_file = url.database + ".%03d" % (max_index + 1)
        shutil.copy(url.database, dest_file)
        log.debug("Created backup database file at: %s" % dest_file)
        pass


def _verify_tables(db):
    try:
        missing_tables = get_missing_tables(db)
        if len(missing_tables) > 0:
            raise DBAdminError("Missing database tables or tables are not updated:\n    %s\nRun 'pegasus-db-admin update %s' to create/update your database."
                % (" \n    ".join(missing_tables), db.get_bind().url))
    except Exception, e:
        raise DBAdminError(e)


def _get_minor_version(version):
    return int(str(float(version)-int(version))[2:])


def _get_max_minor_version(version):
    max_version = 0
    for ver in COMPATIBILITY:
        minor_version = _get_minor_version(COMPATIBILITY[ver])
        if int(COMPATIBILITY[ver]) == version and minor_version > max_version:
            max_version = minor_version
    return max_version
