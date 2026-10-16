// Version and identity of the Fieldproof Gazebo plugin library (task P0-T07).
#pragma once

#include <string>

namespace fieldproof_gz_plugins {

/// The library version, taken from package.xml at build time (release-please keeps it equal to
/// the repository version in version.txt).
std::string Version();

/// Name of the plugin library as it appears in logs and SDF `<plugin>` elements.
std::string LibraryName();

/// `<name> <version>`, the string the plugins log when Gazebo configures them.
std::string Banner();

}  // namespace fieldproof_gz_plugins
