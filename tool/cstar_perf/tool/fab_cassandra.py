###############################################################################
## Fabric file to bootstrap Apache Cassandra on a set of nodes
## 
## Examples:
## 
## Install and configure Cassandra, setup firewall, start seeds, start
## nodes:
##
##  fab -f fab_dse.py configure_hostnames
##  fab -f fab_dse.py configure_firewall
##  fab -f fab_dse.py install_java
##  fab -f fab_dse.py bootstrap
##  fab -f fab_dse.py start:just_seeds=True
##  fab -f fab_dse.py start
## 
##  OR, all in one:
##
##  fab -f fab_cassandra.py bootstrap_all
################################################################################
from StringIO import StringIO
import time
import fabric
from fabric import api as fab
import os
import yaml
from cluster_config import config as cluster_config
import re
import uuid
from util import random_token

fab.env.use_ssh_config = True
fab.env.connection_attempts = 10

# Git repositories
git_repos = [
    ('apache',        'git://github.com/apache/cassandra.git'),
    ('enigmacurry',   'git://github.com/EnigmaCurry/cassandra.git'),
    ('knifewine',     'git://github.com/knifewine/cassandra.git'),
    ('shawnkumar',    'git://github.com/shawnkumar/cassandra.git'),
    ('jbellis',       'git://github.com/jbellis/cassandra.git'),
    ('marcuse',       'git://github.com/krummas/cassandra.git'),
    ('pcmanus',       'git://github.com/pcmanus/cassandra.git'),
    ('belliottsmith', 'git://github.com/belliottsmith/cassandra.git'),
    ('bes',           'git://github.com/belliottsmith/cassandra.git'),
    ('iamaleksey',    'git://github.com/iamaleksey/cassandra.git'),
    ('tjake',         'git://github.com/tjake/cassandra.git'),
    ('carlyeks',      'git://github.com/carlyeks/cassandra.git'),
    ('aweisberg',     'git://github.com/aweisberg/cassandra.git'),
    ('mstump',        'git://github.com/mstump/cassandra.git'),
    ('snazy',         'git://github.com/snazy/cassandra.git')
]

CMD_LINE_HOSTS_SPECIFIED = False
if len(fab.env.hosts) > 0 :
    CMD_LINE_HOSTS_SPECIFIED = True

MAX_CACHED_BUILDS = 10

logback_template = """<configuration scan="true">
  <jmxConfigurator />
  <appender name="FILE" class="ch.qos.logback.core.rolling.RollingFileAppender">
    <file>${cassandra.logdir}/system.log</file>
    <rollingPolicy class="ch.qos.logback.core.rolling.FixedWindowRollingPolicy">
      <fileNamePattern>${cassandra.logdir}/system.log.%i.zip</fileNamePattern>
      <minIndex>1</minIndex>
      <maxIndex>20</maxIndex>
    </rollingPolicy>

    <triggeringPolicy class="ch.qos.logback.core.rolling.SizeBasedTriggeringPolicy">
      <maxFileSize>20MB</maxFileSize>
    </triggeringPolicy>
    <encoder>
      <pattern>%-5level [%thread] %date{ISO8601} %F:%L - %msg%n</pattern>
      <!-- old-style log format
      <pattern>%5level [%thread] %date{ISO8601} %F (line %L) %msg%n</pattern>
      -->
    </encoder>
  </appender>
  
  <appender name="STDOUT" class="ch.qos.logback.core.ConsoleAppender">
    <encoder>
      <pattern>%-5level %date{HH:mm:ss,SSS} %msg%n</pattern>
    </encoder>
  </appender>
        
  <root level="INFO">
    <appender-ref ref="FILE" />
    <appender-ref ref="STDOUT" />
  </root>
  
  <logger name="com.thinkaurelius.thrift" level="ERROR"/>
</configuration>
"""

log4j_template = """
log4j.rootLogger=INFO,stdout,R
log4j.appender.stdout=org.apache.log4j.ConsoleAppender
log4j.appender.stdout.layout=org.apache.log4j.PatternLayout
log4j.appender.stdout.layout.ConversionPattern=%5p %d{HH:mm:ss,SSS} %m%n
log4j.appender.R=org.apache.log4j.RollingFileAppender
log4j.appender.R.maxFileSize=20MB
log4j.appender.R.maxBackupIndex=50
log4j.appender.R.layout=org.apache.log4j.PatternLayout
log4j.appender.R.layout.ConversionPattern=%5p [%t] %d{ISO8601} %F (line %L) %m%n
log4j.appender.R.File=${cassandra.logdir}/system.log
log4j.logger.org.apache.thrift.server.TNonblockingServer=ERROR
"""

################################################################################
### Setup Configuration:
################################################################################
def setup(my_config=None):
    global config

    def __get_balanced_tokens(node_count, partitioner='murmur3'):
        if partitioner == 'murmur3':
            return [str(((2**64 / node_count) * i) - 2**63) 
                    for i in range(node_count)]
        elif partitioner == 'random':
            return [str(i*(2**127/node_count)) for i in range(0, node_count)]
        else:
            raise ValueError('Unknonwn partitioner: %s' % partitioner)

    ########################################
    ### Default config:
    ########################################
    default_config = {
        # The git revision id or tag to use:
        'revision': 'trunk',
        # Override version, tell ant to build with a given version name:
        'override_version': None,
        # Cluster name
        'cluster_name': 'cstar_perf {random_string}'.format(random_string=random_token()),
        # Ant tarball:
        'ant_tarball' : 'http://www.apache.org/dist/ant/binaries/apache-ant-1.8.4-bin.tar.bz2',
        # The user to install as
        'user': 'ryan',
        'partitioner': 'murmur3', #murmur3 or random
        'git_repo': 'git://github.com/apache/cassandra.git',
        # Enable vnodes:
        'use_vnodes': True,
        # Number of vnodes per node. Ignored if use_vnodes == False:
        'num_tokens': 256,
        # Directories:
        'data_file_directories': ['/var/lib/cassandra/data'],
        'commitlog_directory': '/var/lib/cassandra/commitlog',
        'saved_caches_directory': '/var/lib/cassandra/saved_caches',
        'flush_directory': '/var/lib/cassandra/flush',
        # Log file:
        'log_dir': '~/fab/cassandra/logs',
        # Device readahead setting. None means use system default.
        'blockdev_readahead': None,
        # Block devices that above readahead setting affects:
        'block_devices': [],
        # Force loading JNA on <2.0 (2.1+ has it by default):
        'use_jna': True,
        # Extra environment settings to prepend to cassandra-env.sh:
        'env': '',
        'java_home': '~/fab/java'
    }

    public_ips = "node0, node1, node2, node3"
    private_ips = "192.168.1.141,192.168.1.145,192.168.1.143,192.168.1.133"
    public_ips = public_ips.replace(" ","").split(",")
    private_ips = private_ips.replace(" ","").split(",")

    tokens = __get_balanced_tokens(len(public_ips), default_config['partitioner'])    
    default_config.setdefault('hosts', {})
    first_node = True
    for i, public, private, token in zip(
            xrange(len(public_ips)), public_ips, private_ips, tokens):
        if not default_config['hosts'].has_key(public):
            default_config['hosts'][public] = {
                #Local hostname to give the host: 
                'hostname': 'node%s' % i,
                #Internal IP address of the host:
                'internal_ip': private,
                }
            if not default_config['use_vnodes']:
                default_config['hosts'][public]['initial_token'] = token
            # Make the first node a seed:
            if first_node:
                default_config['hosts'][public]['seed'] = True
                first_node = False

    ########################################
    ### End default config
    ########################################

    # Use default values where not specified:
    if not my_config:
        config = default_config
    else:
        config = dict(default_config.items() + my_config.items())

    # Retokenize if needed:
    for node in config['hosts'].values():
        if not config['use_vnodes'] and not node.has_key('initial_token'):
            # At least one node was missing it's token, retokenize all the nodes:
            tokens = __get_balanced_tokens(len(config['hosts']), config['partitioner'])
            for node,token in zip(config['hosts'].values(), tokens):
                node['initial_token'] = token
            break

    #Aggregate those nodes which are seeds into a single list:
    config['seeds'] = [v.get('external_ip', v['internal_ip']) for v in config['hosts'].values() 
                      if v.get('seed',False)]

    # Tell fabric which hosts to use, unless some were 
    # specified on the command line:
    if not CMD_LINE_HOSTS_SPECIFIED:
        fab.env.hosts = [h for h in config['hosts']]
    fab.env.user = config['user']

## Setup default configuration:
# First call without arguments sets up default config:
setup()
# Second call with cluster_config argument sets up further defaults
# for the configured cluster:
setup(cluster_config)

def get_cassandra_config_options():
    """Parse Cassandra's Config class to get all possible config values. 

    Unfortunately, some are hidden from the default cassandra.yaml file, so this appears the only way to do this."""

    # Get Jython helper : 
    jython_status = fab.run('test -f ~/fab/jython.jar', quiet=True)
    if jython_status.return_code > 0:
        fab.run("wget http://search.maven.org/remotecontent?filepath=org/python/jython-standalone/2.7-b1/jython-standalone-2.7-b1.jar -O ~/fab/jython.jar")

    pythonpath = "$HOME/fab/cassandra/build/classes/main"
    classpath = ":".join([pythonpath, "$HOME/fab/cassandra/lib/*", "$HOME/fab/jython.jar"])
    cmd = '{java_home}/bin/java -cp "{classpath}" -Dpython.path="{pythonpath}" org.python.util.jython -c "import org.apache.cassandra.config.Config as Config; print dict(Config.__dict__).keys()"'.format(java_home=config['java_home'], **locals())

    out = fab.run(cmd, combine_stderr=False)
    if out.failed:
        fab.abort('Failed to run Jython Config parser : ' + out.stderr)
    opts = yaml.load(out)
    p = re.compile("^[a-z][^A-Z]*$")
    return [o for o in opts if p.match(o)]
    
@fab.parallel
def bootstrap(git_fetch=True):
    """Install and configure Cassandra on each host
    
    Returns the git id for the version checked out.
    """
    revision = config['revision']
    partitioner = config['partitioner']

    fab.run('mkdir -p fab')

    #Fetch latest git changes:
    if git_fetch:
        git_checkout_status = fab.run('test -d ~/fab/cassandra.git', quiet=True)
        if git_checkout_status.return_code > 0:
            fab.run('git init --bare ~/fab/cassandra.git')
            for name,url in git_repos:
                fab.run('git --git-dir=$HOME/fab/cassandra.git remote add {name} {url}'
                        .format(name=name, url=url), quiet=True)

        for name,url in reversed(git_repos):
            fab.run('git --git-dir=$HOME/fab/cassandra.git fetch {name}'.format(name=name))
            # TODO: What did this used to do? This used to be necessary,
            # but now it appears to delete branches: 
            # fab.run('git --git-dir=$HOME/fab/cassandra.git fetch -fup {name} +refs/*:refs/*'
            #         .format(name=name))

    fab.run('rm -rf ~/fab/cassandra')

    # Find the SHA for the revision requested:
    git_id = fab.run('git --git-dir=$HOME/fab/cassandra.git rev-parse {revision}'.format(revision=revision)).strip()
    # Check if we have already built the revision requested.
    # This speeds up consecutive runs of the same revision.
    fab.run('mkdir -p ~/fab/cassandra_builds')
    test_already_built = fab.run('test -d ~/fab/cassandra_builds/{git_id}'.format(git_id=git_id), quiet=True)
    if test_already_built.return_code == 0:
        # Copy previously built Cassandra
        fab.run('cp -a ~/fab/cassandra_builds/{git_id} ~/fab/cassandra'.format(git_id=git_id))
    else:
        # Build Cassandra
        # Checkout revision/tag:
        fab.run('mkdir ~/fab/cassandra')
        fab.run('git --git-dir=$HOME/fab/cassandra.git archive %s |'
                ' tar x -C ~/fab/cassandra' % revision)
        fab.run('echo -e \'%s\\n%s\\n%s\' > ~/fab/cassandra/0.GIT_REVISION.txt' % 
                (revision, git_id, config.get('log','')))

        fab.run('JAVA_HOME={java_home} ~/fab/ant/bin/ant -f ~/fab/cassandra/build.xml clean'.format(java_home=config['java_home']))
        if config['override_version'] is not None:
            fab.run('JAVA_HOME={java_home} ~/fab/ant/bin/ant -f ~/fab/cassandra/build.xml -Dversion={version}'.format(java_home=config['java_home'], version=config['override_version']))
        else:
            fab.run('JAVA_HOME={java_home} ~/fab/ant/bin/ant -f ~/fab/cassandra/build.xml'.format(java_home=config['java_home']))

        # Archive this build for future runs:
        fab.run('cp -a ~/fab/cassandra ~/fab/cassandra_builds/{git_id}'.format(git_id=git_id))

        # Remove old builds:
        num_builds = int(fab.run('ls -1 ~/fab/cassandra_builds | wc -l').strip())
        if num_builds > MAX_CACHED_BUILDS:
            fab.run('ls -t1 ~/fab/cassandra_builds | tail -n {num_to_delete} | xargs -iXX rm -rf ~/fab/cassandra_builds/XX'.format(
                num_to_delete=num_builds-MAX_CACHED_BUILDS))

    # Get host config:
    try:
        cfg = config['hosts'][fab.env.host]
    except KeyError:
        # If host has no config, don't configure it. This is used for
        # compiling the source on the controlling node which is
        # usually not a part of the cluster.
        return git_id

    #Ensure JNA is available:
    if config['use_jna']:
        # Check if JNA already exists:
        jna_exists = fab.run('ls ~/fab/cassandra/lib/jna*.jar', quiet=True)
        if jna_exists.return_code != 0:
            # Symlink system JNA to cassandra lib dir:
            jna_candidates = ['/usr/share/java/jna/jna.jar', '/usr/share/java/jna.jar']
            for jar in jna_candidates:
                if fab.run('ls {jar}'.format(jar=jar), quiet=True).return_code == 0:
                    fab.run('ln -s {jar} ~/fab/cassandra/lib/jna.jar'.format(jar=jar))
                    break
            else:
                raise AssertionError('Could not force JNA loading, no JNA jar found.')
    else:
        fab.run('rm -f ~/fab/cassandra/lib/jna*')

    # Configure cassandra.yaml:
    conf_file = StringIO()
    fab.get('~/fab/cassandra/conf/cassandra.yaml', conf_file)
    conf_file.seek(0)
    cass_yaml = yaml.load(conf_file.read())

    # Get the canonical list of options from the c* source code:
    cstar_config_opts = get_cassandra_config_options()

    # Cassandra YAML values can come from two places: 
    # 1) Set as options at the top level of the config. This is how
    # legacy cstar_perf did it. These are unvalidated:
    for option, value in config.items():
        if option in cstar_config_opts:
            cass_yaml[option] = value
    # 2) Set in the second level 'yaml' dictionary. This is how the
    # frontend and bootstrap.py does it. These take precedence over
    # the #1 style and are always validated for typos / invalid options.
    for option, value in config.get('yaml', {}).items():
        if option not in cstar_config_opts:
            raise ValueError('Unknown C* yaml option: {}'.format(option))
        cass_yaml[option] = value

    if 'num_tokens' not in config.get('yaml', {}):
        if config.get('use_vnodes', True):
            cass_yaml['num_tokens'] = config['num_tokens']
        else:
            cass_yaml['initial_token'] = cfg['initial_token']
            cass_yaml['num_tokens'] = 1
    cass_yaml['listen_address'] = cfg['internal_ip']
    cass_yaml['broadcast_address'] = cfg.get('external_ip', cfg['internal_ip'])
    cass_yaml['seed_provider'][0]['parameters'][0]['seeds'] =  ",".join(config['seeds'])
    if partitioner == 'random':
        cass_yaml['partitioner'] = 'org.apache.cassandra.dht.RandomPartitioner'
    elif partitioner == 'murmur3':
        cass_yaml['partitioner'] = 'org.apache.cassandra.dht.Murmur3Partitioner'
    cass_yaml['rpc_address'] = cfg['internal_ip']

    #Configure Topology:
    if not config.has_key('endpoint_snitch'):
        for node in config['hosts'].values():
            if node.get('datacenter',False):
                config['endpoint_snitch'] = "GossipingPropertyFileSnitch"
                cass_yaml['auto_bootstrap'] = False
                break
        else:
            config['endpoint_snitch'] = "SimpleSnitch"

    if config['endpoint_snitch'] == 'PropertyFileSnitch':
        cass_yaml['endpoint_snitch'] = 'PropertyFileSnitch'
        fab.run("echo 'default=dc1:r1' > fab/cassandra/conf/cassandra-topology.properties")
        for node in config['hosts'].values():
            line = '%s=%s:%s' % (node['external_ip'], node.get('datacenter', 'dc1'), node.get('rack', 'r1'))
            fab.run("echo '%s' >> fab/cassandra/conf/cassandra-topology.properties" % line)
    if config['endpoint_snitch'] == "GossipingPropertyFileSnitch":
        cass_yaml['endpoint_snitch'] = 'GossipingPropertyFileSnitch'
        fab.run("echo 'dc={dc}\nrack={rack}' > fab/cassandra/conf/cassandra-rackdc.properties".format(
            dc=cfg.get('datacenter','dc1'), rack=cfg.get('rack','r1')))

    # Save config:
    conf_file = StringIO()
    conf_file.write(yaml.safe_dump(cass_yaml, encoding='utf-8', allow_unicode=True))
    conf_file.seek(0)
    fab.put(conf_file, '~/fab/cassandra/conf/cassandra.yaml')

    # Configure logback:
    logback_conf = StringIO()
    # Get absolute path to log dir:
    log_dir = fab.run("readlink -m {log_dir}".format(log_dir=config['log_dir']))
    logback_conf.write(logback_template.replace("${cassandra.logdir}",log_dir))
    logback_conf.seek(0)
    fab.put(logback_conf, '~/fab/cassandra/conf/logback.xml')

    # Configure log4j:
    log4j_conf = StringIO()
    log4j_conf.write(log4j_template.replace("${cassandra.logdir}",log_dir))
    log4j_conf.seek(0)
    fab.put(log4j_conf, '~/fab/cassandra/conf/log4j-server.properties')

    # Copy fincore utility:
    fincore_script = os.path.join(os.path.dirname(os.path.realpath(__file__)),'fincore_capture.py')
    fab.put(fincore_script, '~/fab/fincore_capture.py')
    return git_id

@fab.parallel
def destroy(leave_data=False):
    """Uninstall Cassandra and clean up data and logs"""
    # We used to have a better pattern match for the Cassandra
    # process, but it got fragile if you put too many JVM params. 
    fab.run('killall -9 java', quiet=True)
    fab.run('pkill -f "python.*fincore_capture"', quiet=True)
    fab.run('rm -rf fab/cassandra')
    fab.run('rm -rf fab/scripts')
    fab.run('rm -f fab/nohup.log')
    if not leave_data:
        for d in config['data_file_directories']:
            fab.run('rm -rf {data}/*'.format(data=d))
        fab.run('rm -rf {saved_caches_directory}/*'.format(saved_caches_directory=config['saved_caches_directory']))
        fab.run('rm -rf {commitlog}/*'.format(commitlog=config['commitlog_directory']))
        fab.run('rm -rf {flushdir}/*'.format(flushdir=config['flush_directory']))
    fab.run('rm -rf {log_dir}'.format(log_dir=config['log_dir']))
    fab.run('rm -f /tmp/fincore.stats.log')

@fab.parallel
def start():
    """Start casssandra nodes"""
    # Get host config:
    cfg = config['hosts'][fab.env.host]

    # Place environment file on host:
    env = config.get('env', '')

    if isinstance(env, list) or isinstance(env, tuple):
        env = "\n".join(env)
    env += "\n"
    if not config['use_jna']:
        env = 'JVM_EXTRA_OPTS=-Dcassandra.boot_without_jna=true\n\n' + env
    # Turn on GC logging:
    fab.run("mkdir -p ~/fab/cassandra/logs")
    log_dir = fab.run("readlink -m {log_dir}".format(log_dir=config['log_dir']))
    env = "JVM_OPTS=\"$JVM_OPTS -Djava.rmi.server.hostname={hostname} -Xloggc:{log_dir}/gc.log\"\n\n".format(
        hostname=fab.env.host, log_dir=log_dir) + env

    env_script = "{name}.sh".format(name=uuid.uuid1())
    env_file = StringIO(env)
    fab.run('mkdir -p ~/fab/scripts')
    fab.put(env_file, '~/fab/scripts/{env_script}'.format(env_script=env_script))

    if len(env_script) > 0:
        fab.run('echo >> ~/fab/scripts/{env_script}'.format(**locals()))
        fab.run('cat ~/fab/cassandra/conf/cassandra-env.sh >> ~/fab/scripts/{env_script}'.format(**locals()))
        fab.run('cp ~/fab/scripts/{env_script} ~/fab/cassandra/conf/cassandra-env.sh'.format(**locals()))

    fab.puts("Starting Cassandra..")
    cmd = 'JAVA_HOME={java_home} nohup ~/fab/cassandra/bin/cassandra'.format(java_home=config['java_home'])
    fab.run(cmd)

@fab.parallel
def stop(clean=True):
    if clean:
        fab.run('pkill -f "java.*org.apache.*.CassandraDaemon"', quiet=True)
    else:
        fab.run('pkill -9 -f "java.*org.apache.*.CassandraDaemon"', quiet=True)

def ensure_running(retries=15, wait=10):
    """Ensure cassandra is running on all nodes.
    Runs 'nodetool ring' on a single node continuously until it 
    reaches the specified number of retries.
    
    INTENDED TO BE RUN ON ONE NODE, NOT ALL.
    """
    time.sleep(15)
    for attempt in range(retries):
        ring = StringIO(fab.run('JAVA_HOME={java_home} ~/fab/cassandra/bin/nodetool ring'.format(java_home=config['java_home'])))
        broadcast_ips = [x.get('external_ip', x['internal_ip']) for x in config['hosts'].values()]
        nodes_up = dict((host,False) for host in broadcast_ips)
        for line in ring:
            for host in broadcast_ips:
                if host in line and " Up " in line:
                    nodes_up[host] = True
        for node,up in nodes_up.items():
            if not up:
                fab.puts("Node is not up (yet): %s" % node)
        if False not in nodes_up.values():
            fab.puts("All nodes available!")
            return
        fab.puts("waiting %d seconds to try again.." % wait)
        time.sleep(wait)
    else:
        fab.abort("Timed out waiting for all nodes to startup")

@fab.parallel
def ensure_stopped(retries=15, wait=10):
    """Ensure cassandra is stopped on all nodes.
    Checks continuously until it reaches the specified number of retries."""
    for attempt in range(retries):
        pgrep = fab.run('pgrep -f "java.*org.apache.*.CassandraDaemon"', quiet=True)
        if pgrep.return_code != 0:
            fab.puts('Cassandra shutdown.')
            return
        fab.puts("waiting %d seconds to try again.." % wait)
        time.sleep(wait)
    else:
        fab.abort("Timed out waiting for all nodes to stop")

@fab.parallel
def install_java(packages=None):
    # Try to get the os distribution:
    dist = fab.run('lsb_release -is', quiet=True)
    if dist.return_code != 0:
        dist = fab.run('cat /etc/redhat-release', quiet=True)
    if dist.startswith('CentOS'):
        if not packages:
            packages = ['java-1.7.0-openjdk.x86_64', 
                        'java-1.7.0-openjdk-devel.x86_64']
        cmd = 'yum -y install {package}'
    elif dist.startswith('Ubuntu'):
        if not packages:
            packages = ['openjdk-7-jdk']
        fab.run('apt-get update')
        cmd = 'apt-get -y install {package}'
    else:
        raise RuntimeError('Unknown distribution: %s' % dist)
    for package in packages:
        fab.run(cmd.format(package=package))
        
@fab.parallel
def configure_hostnames():
    # Get host config:
    cfg = config['hosts'][fab.env.host]
    fab.run('hostname {0}'.format(cfg['hostname']))
    fab.run('echo "127.0.0.1       localhost.localdomain   localhost"'
            ' > /etc/hosts')
    fab.run('echo "::1       localhost.localdomain   localhost" >> /etc/hosts')
    for cfg in config['hosts'].values():
        fab.run('echo "{ip}     {hostname}" >> /etc/hosts'.format(
                ip=cfg['internal_ip'], hostname=cfg['hostname']))

@fab.parallel
def copy_logs(local_directory):
    cfg = config['hosts'][fab.env.host]
    host_log_dir = os.path.join(local_directory, cfg['hostname'])
    os.mkdir(host_log_dir)
    fab.get(os.path.join(config['log_dir'],'*'), host_log_dir)

@fab.parallel
def start_fincore_capture(interval=10):
    """Start fincore_capture utility on each node"""
    fab.puts("Starting fincore_capture daemon...")
    fab.run('python2.7 fab/fincore_capture.py -i {interval}'.format(interval=interval))

@fab.parallel
def stop_fincore_capture():
    """Stop fincore_capture utility on each node"""
    fab.puts("Stopping fincore_capture.")
    fab.run('pkill -f ".*python.*fincore_capture"', quiet=True)

@fab.parallel
def copy_fincore_logs(local_directory):
    cfg = config['hosts'][fab.env.host]
    location = os.path.join(local_directory, "fincore.{host}.log".format(host=cfg['hostname']))
    fab.get('/tmp/fincore.stats.log', location)

@fab.parallel
def whoami():
    fab.run('whoami')

@fab.parallel
def add_git_remotes():
    for name,url in git_repos:
        fab.run('git --git-dir=$HOME/fab/cassandra.git remote add {name} {url}'
                .format(name=name, url=url), quiet=True)

@fab.parallel
def copy_root_setup():
    fab.run('ln -s ~/fab/apache-ant-1.9.2 ~/fab/ant')

@fab.parallel
def set_device_read_ahead(read_ahead, devices):
    with fab.settings(user='root'):
        for device in devices:
            fab.run('blockdev --setra {read_ahead} {device}'.format(read_ahead=read_ahead,device=device))

@fab.parallel
def build_jbod_drives(device_mounts, md_device='/dev/md/striped', filesystem='ext4'):
    """Build a jbod configuration for drives
    
    device_mounts - a dictionary of device names -> mountpoints. Alternatively, can be a string representation.
        fab -f fab_cassandra.py build_jbod_drives:"{'/dev/sdb1':'/mnt/d1'\,'/dev/sdc1':'/mnt/d2'\,'/dev/sdd1':'/mnt/d3'\,'/dev/sde1':'/mnt/d4'\,'/dev/sdf1':'/mnt/d5'\,'/dev/sdg1':'/mnt/d6'\,'/dev/sdh1':'/mnt/d7'}"
    md_device - The mdadm striped device that may need to be destoyed first.

    Formats the devices and mounts the filesystems.
    """
    with fab.settings(user='root'):
        if isinstance(device_mounts, basestring):
            device_mounts = eval(device_mounts)

        # Destroy the mdadm striped device:
        fab.run('umount {md_device} && mdadm --stop {md_device}'.format(**locals()), quiet=True)

        mounted = fab.run('mount',quiet=True)
        for device in [d for d in device_mounts.keys() if d in mounted]:
            # Unmount devices:
            fab.run('umount -f {device}'.format(**locals()))

        # Format in parallel :
        fab.run('echo -e "{devices}" | xargs -n 1 -P {num} -iXX mkfs -t {filesystem} XX'.format(devices="\n".join(device_mounts.keys()), filesystem=filesystem, num=len(device_mounts)))

        for device,mountpoint in device_mounts.items():
            # Mount:
            fab.run('mount {device} {mountpoint}'.format(**locals()))
            fab.run('chmod 777 {mountpoint}'.format(**locals()))

@fab.parallel
def build_striped_drives(devices, mount_point='/mnt/striped', filesystem='ext4', chunk_size=64, md_device='/dev/md/striped'):
    """Build a striped array of drives with mdadm

    devices - a list of device names to include in the array 
            - altenatively, a string delimitted with semicolons for use on the command line:

        fab -f fab_cassandra.py build_striped_drives:'/dev/sdb1;/dev/sdc1;/dev/sdd1;/dev/sde1;/dev/sdf1;/dev/sdg1;/dev/sdh1',/mnt/striped
    """
    with fab.settings(user='root'):
        if isinstance(devices, basestring):
            devices = devices.split(";")

        # Unmount devices:
        mounted = fab.run('mount',quiet=True)
        for device in [d for d in devices + [mount_point] if d in mounted]:
            fab.run('umount -f {device}'.format(**locals()))

        # Stop any existing array:
        fab.run('mdadm --stop {md_device}'.format(**locals()),quiet=True)
        
        # Create new array:
        fab.run('mdadm --create {md_device} -R --verbose --level=0 --metadata=1.2 --chunk={chunk_size} --raid-devices={num} {devices}'.format(
            md_device=md_device, num=len(devices), chunk_size=chunk_size, devices=" ".join(devices)))

        # Format it:
        fab.run('mkfs -t {filesystem} {md_device}'.format(**locals()))

        # Mount it:
        fab.run('mkdir -p {mount_point} && mount {md_device} {mount_point}'.format(**locals()))

        fab.run('chmod 777 {mount_point}'.format(**locals()))

@fab.parallel
def bash(script):
    """Run a bash script on the host"""
    script = StringIO(script)
    fab.run('mkdir -p ~/fab/scripts')
    script_path = '~/fab/scripts/{script_name}.sh'.format(script_name=uuid.uuid1())
    fab.put(script, script_path)
    fab.run('bash {script_path}'.format(script_path=script_path))
