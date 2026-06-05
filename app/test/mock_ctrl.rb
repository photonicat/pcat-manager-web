require 'socket'

begin
  server = UNIXServer.new('/tmp/pcat-manager.sock')
  puts "Waing client ..."

  while true
    socket = server.accept
    puts "a client connected."

    # read the headers, at least
    while true
      puts "socket:#{socket.inspect}"
      a = socket.gets
      if a.nil? || a.size == 0
        puts "Current client disconntected."
        break
      end  
      puts "RECV #{a.size}:#{a}"
      sleep 1
    end
  end

  server.close
ensure
  File.unlink('/tmp/pcat-manager.sock')
end