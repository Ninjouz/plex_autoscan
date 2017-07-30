import logging
import os
import sqlite3
import time

try:
    from shlex import quote as cmd_quote
except ImportError:
    from pipes import quote as cmd_quote
import requests

import utils

logger = logging.getLogger("PLEX")
logger.setLevel(logging.DEBUG)

def build_cmd(config, section, scan_path, scan_op):
    # build plex scanner command
    logger.info("Building Plex Scan Command")
    if os.name == 'nt':
        final_cmd = '""%s" --scan --refresh --section %s --directory "%s""' \
                    % (config['PLEX_SCANNER'], str(section), scan_path)
    else:
        cmd = 'export LD_LIBRARY_PATH=' + config['PLEX_LD_LIBRARY_PATH'] + ';'
        if not config['USE_DOCKER']:
            cmd += 'export PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR=' + config['PLEX_SUPPORT_DIR'] + ';'
        if scan_op == 'scan':
            cmd += config['PLEX_SCANNER'] + ' --scan --refresh --section ' + str(section) + ' --directory ' + cmd_quote(
                scan_path)
        elif scan_op == 'analyze':
            media_id = get_media_id(config, scan_path)
            cmd += config['PLEX_SCANNER'] + ' --analyze -o ' + str(media_id)
        elif scan_op == 'deep':
            media_id = get_media_id(config, scan_path)
            cmd += config['PLEX_SCANNER'] + ' --analyze-deeply -o ' + str(media_id)

        if config['USE_DOCKER']:
            final_cmd = 'docker exec -i %s bash -c %s' % (cmd_quote(config['DOCKER_NAME']), cmd_quote(cmd))
        elif config['USE_SUDO']:
            final_cmd = 'sudo -u %s bash -c %s' % (config['PLEX_USER'], cmd_quote(cmd))
        else:
            final_cmd = cmd

    return final_cmd

def scan(config, lock, path, scan_for, section, scan_type):
    scan_path = ""

    # sleep for delay
    if config['SERVER_SCAN_DELAY']:
        logger.info("Scan request for '%s', scan delay of %d seconds. Sleeping...", path, config['SERVER_SCAN_DELAY'])
        time.sleep(config['SERVER_SCAN_DELAY'])
    else:
        logger.info("Scan request for '%s'", path)

    # check file exists
    if scan_for == 'radarr' or scan_for == 'sonarr_dev' or scan_for == 'manual':
        checks = 0
        check_path = utils.map_pushed_path_file_exists(config, path)
        while True:
            checks += 1
            if os.path.exists(check_path):
                logger.info("File '%s' exists on check %d of %d.", check_path, checks, config['SERVER_MAX_FILE_CHECKS'])
                scan_path = os.path.dirname(path).strip()
                break
            elif checks >= config['SERVER_MAX_FILE_CHECKS']:
                logger.warning("File '%s' exhausted all available checks, aborting scan request.", check_path)
                return
            else:
                logger.info("File '%s' did not exist on check %d of %d, checking again in 60 seconds.", check_path,
                            checks,
                            config['SERVER_MAX_FILE_CHECKS'])
                time.sleep(60)

    else:
        # old sonarr doesnt pass the sonarr_episodefile_path in webhook, so we cannot check until this is corrected.
        scan_path = path.strip()

    # invoke plex scanner
    logger.debug("Waiting for turn in the scan request backlog...")
    with lock:
        logger.info("Scan request is now being processed")
        # wait for existing scanners being ran by plex
        if config['PLEX_WAIT_FOR_EXTERNAL_SCANNERS']:
            scanner_name = os.path.basename(config['PLEX_SCANNER']).replace('\\', '')
            if not utils.wait_running_process(scanner_name):
                logger.warning(
                    "There was a problem waiting for existing '%s' process(s) to finish, aborting scan.", scanner_name)
                return
            else:
                logger.info("No '%s' processes were found.", scanner_name)

        # begin scan
        logger.info("Starting Plex Scanner To Scan")
        final_cmd = build_cmd(config, section, scan_path, 'scan')
        logger.debug(final_cmd)
        utils.run_command(final_cmd.encode("utf-8"))
        logger.info("Finished scan!")
        if config['PLEX_ANALYZE']:
            logger.info("Starting Plex Scanner To Analyze")
            final_cmd = build_cmd(config, section, scan_path, 'analyze')
            logger.debug(final_cmd)
            utils.run_command(final_cmd.encode("utf-8"))
            logger.info("Finished analyze!")
        if config['PLEX_DEEP_ANALYZE']:
            logger.info("Starting Plex Scanner To Deep Analyze")
            final_cmd = build_cmd(config, section, scan_path, 'deep')
            logger.debug(final_cmd)
            utils.run_command(final_cmd.encode("utf-8"))
            logger.info("Finished deep analyze!")            
        # empty trash if configured
        if config['PLEX_EMPTY_TRASH'] and config['PLEX_TOKEN'] and config['PLEX_EMPTY_TRASH_MAX_FILES']:
            logger.info("Checking deleted item count in 5 seconds...")
            time.sleep(5)

            # check deleted item count, don't proceed if more than this value
            deleted_items = get_deleted_count(config)
            if deleted_items > config['PLEX_EMPTY_TRASH_MAX_FILES']:
                logger.warning("There were %d deleted files, skipping emptying trash for section %s", deleted_items,
                               section)
                return
            if deleted_items == -1:
                logger.error("Could not determine deleted item count, aborting emptying trash")
                return
            if not config['PLEX_EMPTY_TRASH_ZERO_DELETED'] and not deleted_items and scan_type != 'Upgrade':
                logger.info("Skipping emptying trash as there were no deleted items")
                return
            logger.info("Emptying trash to clear %d deleted items", deleted_items)
            empty_trash(config, str(section))

    return


def show_sections(config):
    if os.name == 'nt':
        final_cmd = '""%s" --list"' % config['PLEX_SCANNER']
    else:
        cmd = 'export LD_LIBRARY_PATH=' + config['PLEX_LD_LIBRARY_PATH'] + ';'
        if not config['USE_DOCKER']:
            cmd += 'export PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR=' + config['PLEX_SUPPORT_DIR'] + ';'
        cmd += config['PLEX_SCANNER'] + ' --list'

        if config['USE_DOCKER']:
            final_cmd = 'docker exec -it %s bash -c %s' % (cmd_quote(config['DOCKER_NAME']), cmd_quote(cmd))
        elif config['USE_SUDO']:
            final_cmd = 'sudo -u %s bash -c "%s"' % (config['PLEX_USER'], cmd)
        else:
            final_cmd = cmd
    logger.info("Using Plex Scanner")
    logger.debug(final_cmd)
    os.system(final_cmd)


def empty_trash(config, section):
    for control in config['PLEX_EMPTY_TRASH_CONTROL_FILES']:
        if not os.path.exists(control):
            logger.info("Skipping emptying trash as control file does not exist: '%s'", control)
            return

    if len(config['PLEX_EMPTY_TRASH_CONTROL_FILES']):
        logger.info("Control file(s) exist!")

    try:
        resp = requests.put('%s/library/sections/%s/emptyTrash?X-Plex-Token=%s' % (
            config['PLEX_LOCAL_URL'], section, config['PLEX_TOKEN']), data=None)
        if resp.status_code == 200:
            logger.info("Trash cleared for section %s", section)
        else:
            logger.error("Unexpected response status_code for empty trash request: %d", resp.status_code)

    except Exception as ex:
        logger.exception("Exception while sending empty trash request: ")
    return


def get_deleted_count(config):
    try:
        conn = sqlite3.connect(config['PLEX_DATABASE_PATH'])
        c = conn.cursor()
        deleted_metadata = c.execute('SELECT count(*) FROM metadata_items WHERE deleted_at IS NOT NULL').fetchone()[0]
        deleted_media_parts = c.execute('SELECT count(*) FROM media_parts WHERE deleted_at IS NOT NULL').fetchone()[0]
        conn.close()
        return int(deleted_metadata) + int(deleted_media_parts)
    except Exception as ex:
        logger.exception("Exception retrieving deleted item count from database: ")
        return -1


def get_media_id(config, media_path):
    try:
        conn = sqlite3.connect(config['PLEX_DATABASE_PATH'])
        c = conn.cursor()
        query = "select media_item_id from media_parts where file like '%s%%'" %(media_path)
        media_item_id = c.execute(query).fetchone()[0]
        conn.close()
        return int(media_item_id)
    except Exception as ex:
        logger.exception("Exception retrieving media_item_id from database: ")
        return -1