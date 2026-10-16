// Unit tests for the plugin library's version helpers (task P0-T07).
#include "fieldproof_gz_plugins/version.hpp"

#include <gtest/gtest.h>

#include <regex>
#include <string>

namespace {

TEST(Version, IsSemver) {
  const std::regex semver{R"(\d+\.\d+\.\d+([-+].*)?)"};
  EXPECT_TRUE(std::regex_match(fieldproof_gz_plugins::Version(), semver))
      << fieldproof_gz_plugins::Version();
}

TEST(Version, LibraryNameMatchesPackage) {
  EXPECT_EQ(fieldproof_gz_plugins::LibraryName(), "fieldproof_gz_plugins");
}

TEST(Version, BannerJoinsNameAndVersion) {
  const std::string banner = fieldproof_gz_plugins::Banner();
  EXPECT_EQ(banner, fieldproof_gz_plugins::LibraryName() + " " + fieldproof_gz_plugins::Version());
}

}  // namespace
