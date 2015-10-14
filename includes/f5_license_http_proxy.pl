#!/usr/bin/perl
#
# John Gruber (j.gruber@f5.com)
#
# HTTP proxy shim for the SOAPLicenseClient
#
# license-proxy <local_port> <proxy_host>:<proxy_port>
#

use warnings;
use strict;

use IO::Socket::INET;
use IO::Select;

my $ioset = IO::Select->new;
my %socket_map;

my $debug = 0;

sub new_conn {
    my ($host, $port) = @_;
    return IO::Socket::INET->new(
        PeerAddr => $host,
        PeerPort => $port
    ) || die "Unable to connect to $host:$port: $!";
}

sub new_server {
    my ($host, $port) = @_;
    my $server = IO::Socket::INET->new(
        LocalAddr => $host,
        LocalPort => $port,
        ReuseAddr => 1,
        Listen    => 100
    ) || die "Unable to listen on $host:$port: $!";
}

sub new_connection {
    my $server = shift;
    my $proxy_host = shift;
    my $proxy_port = shift;
    my $activation_host = shift;
    my $activation_port = shift;

    my $client = $server->accept;
    my $client_ip = client_ip($client);

    print "Connection from $client_ip accepted.\n" if $debug;

    my $remote = new_conn($remote_host, $remote_port);
    my $data = "CONNECT $activate_host:$activate_port HTTP/1.1\n";
    $data .= "User-Agent: F5LicenseProxy\n";
    $data .= "Proxy-Connection: close\n";
    $data .= "Connection: close\n";
    $data .= "Host: $activate_host:$activate_port\n\n";
    print $data if $debug; 
    $remote->send($data);
    while (my $line = <$remote>) {
        $line =~ s/\r|\n//g;
        if($line eq "") {
			print "Proxying client to $activate_host:$activate_port\n" if $debug;
            last;
        } else {
            print "Response: $line\n" if $debug;    
        }
    }
    $ioset->add($client);
    $ioset->add($remote);

    $socket_map{$client} = $remote;
    $socket_map{$remote} = $client;
}

sub close_connection {
    my $client = shift;
    my $client_ip = client_ip($client);
    my $remote = $socket_map{$client};
    
    $ioset->remove($client);
    $ioset->remove($remote);

    delete $socket_map{$client};
    delete $socket_map{$remote};

    $client->close;
    $remote->close;

    print "Connection from $client_ip closed.\n" if $debug;
}

sub client_ip {
    my $client = shift;
    return inet_ntoa($client->sockaddr);
}

die "Usage: $0 <local port> <proxy_host>:<proxy_port> <activation_host>:<activation_port>" unless @ARGV == 3;

my $local_port = shift;
my ($remote_host, $remote_port) = split ':', shift();
my ($activation_host, $activation_port) = split ':', shift();


print "Starting a server on 127.0.0.1:$local_port\n" if $debug;
my $server = new_server('127.0.0.1', $local_port);
$ioset->add($server);

while (1) {
    for my $socket ($ioset->can_read) {
        if ($socket == $server) {
            new_connection($server, $remote_host, $remote_port, $activation_host, $activation_port);
        }
        else {
            next unless exists $socket_map{$socket};
            my $remote = $socket_map{$socket};
            my $buffer;
            my $read = $socket->sysread($buffer, 4096);
            if ($read) {
                $remote->syswrite($buffer);
            }
            else {
                close_connection($socket);
            }
        }
    }
}

