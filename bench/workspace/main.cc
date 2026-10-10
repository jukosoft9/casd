#include <iostream>
#include <string>
#include <vector>

#include "absl/flags/flag.h"
#include "absl/flags/parse.h"
#include "records.h"

ABSL_FLAG(bool, strict, false, "exit non-zero on the first unparseable line");

int main(int argc, char** argv) {
  absl::ParseCommandLine(argc, argv);
  std::vector<records::Record> parsed;
  std::string line;
  while (std::getline(std::cin, line)) {
    absl::StatusOr<records::Record> record = records::ParseLine(line);
    if (record.ok()) {
      parsed.push_back(*record);
    } else if (absl::GetFlag(FLAGS_strict)) {
      std::cerr << record.status().message() << "\n";
      return 1;
    }
  }
  std::cout << records::Summary(parsed) << "\n";
  return 0;
}
